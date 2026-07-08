'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import DuplicateCard from '@/components/DuplicateCard'
import DuplicateDetail from '@/components/DuplicateDetail'
import AppNav from '@/components/AppNav'

interface Scan {
  id: string
  object_type: string
  status: string
  records_scanned: number
  duplicates_found: number
  created_at: string
  config?: { dry_run?: boolean }
}

interface DuplicateSet {
  id: string
  confidence: number
  winner_record_id: string
  loser_record_ids: string[]
  winner_data: Record<string, unknown>
  loser_data: Record<string, unknown>[]
  merged_preview: Record<string, unknown>
  excluded: boolean
  merged: boolean
}

interface ReviewClientProps {
  scan: Scan
  userId: string
}

type ConfidenceFilter = 'all' | 'high' | 'medium' | 'low'

export default function ReviewClient({ scan }: ReviewClientProps) {
  const router = useRouter()
  const [duplicateSets, setDuplicateSets] = useState<DuplicateSet[]>([])
  const [selectedSets, setSelectedSets] = useState<Set<string>>(new Set())
  const [expandedSet, setExpandedSet] = useState<DuplicateSet | null>(null)
  const [confidenceFilter, setConfidenceFilter] = useState<ConfidenceFilter>('all')
  const [isLoading, setIsLoading] = useState(true)
  const [isMerging, setIsMerging] = useState(false)
  const [mergeError, setMergeError] = useState<string | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)

  useEffect(() => {
    fetchDuplicates()
  }, [page, scan.id])

  const fetchDuplicates = async () => {
    setIsLoading(true)
    setLoadError(null)
    try {
      const response = await apiFetch(`/scan/${scan.id}/results?page=${page}&per_page=20`)

      if (response.ok) {
        const data = await response.json()
        setDuplicateSets(data.duplicate_sets)
        setTotalPages(data.total_pages)
      } else {
        const data = await response.json().catch(() => ({}))
        setLoadError(data.detail || `Failed to load duplicates (${response.status}).`)
      }
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Failed to load duplicates.')
    } finally {
      setIsLoading(false)
    }
  }

  const toggleSelection = (setId: string) => {
    const newSelected = new Set(selectedSets)
    if (newSelected.has(setId)) {
      newSelected.delete(setId)
    } else {
      newSelected.add(setId)
    }
    setSelectedSets(newSelected)
  }

  const selectAll = () => {
    const filteredSets = getFilteredSets()
    const allIds = new Set(filteredSets.filter(s => !s.excluded).map(s => s.id))
    setSelectedSets(allIds)
  }

  const deselectAll = () => {
    setSelectedSets(new Set())
  }

  const toggleExclude = async (setId: string, excluded: boolean) => {
    // Optimistically update UI
    setDuplicateSets(prev =>
      prev.map(s => s.id === setId ? { ...s, excluded } : s)
    )
    if (excluded) {
      setSelectedSets(prev => {
        const newSet = new Set(prev)
        newSet.delete(setId)
        return newSet
      })
    }

    // Persist to backend
    await apiFetch(`/scan/${scan.id}/duplicate-sets/${setId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ excluded }),
    }).catch(err => console.error('Failed to persist exclude:', err))
  }

  const handleMerge = async () => {
    if (selectedSets.size === 0) return

    setIsMerging(true)
    setMergeError(null)
    try {
      const ids = Array.from(selectedSets)

      // The backend only merges APPROVED sets. Selecting sets and clicking
      // "Merge" IS the human approval, so record it before executing. If ANY
      // approval fails, abort before /execute — otherwise the sets that DID get
      // approved would be left eligible and a later merge could sweep them in
      // unintentionally.
      const approvals = await Promise.allSettled(
        ids.map(setId =>
          apiFetch(`/scan/${scan.id}/duplicate-sets/${setId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision: 'approved' }),
          }).then(r => {
            if (!r.ok) throw new Error(`approve ${setId} failed (${r.status})`)
          })
        )
      )
      const failedApprovals = approvals.filter(a => a.status === 'rejected').length
      if (failedApprovals > 0) {
        setMergeError(
          `Couldn't approve ${failedApprovals} of ${ids.length} selected sets — nothing was merged. Please retry.`
        )
        return
      }

      const response = await apiFetch(`/merge/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          scan_id: scan.id,
          set_ids: ids,
        }),
      })

      if (response.ok) {
        const data = await response.json()
        router.push(`/merge/${data.merge_id}`)
      } else {
        const data = await response.json().catch(() => ({}))
        setMergeError(data.detail || 'Failed to start merge. Please try again.')
      }
    } catch (error) {
      console.error('Failed to start merge:', error)
      setMergeError('Network error. Please check your connection and try again.')
    } finally {
      setIsMerging(false)
    }
  }

  const downloadMatches = async () => {
    setMergeError(null)
    try {
      const response = await apiFetch(`/scan/${scan.id}/export`)
      if (response.ok) {
        const blob = await response.blob()
        const url = window.URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `matches-${scan.id.slice(0, 8)}.xlsx`
        document.body.appendChild(a)
        a.click()
        a.remove()
        window.URL.revokeObjectURL(url)
      } else {
        setMergeError(`Export failed (${response.status}).`)
      }
    } catch (error) {
      setMergeError(error instanceof Error ? `Export failed: ${error.message}` : 'Export failed.')
    }
  }

  const getFilteredSets = () => {
    return duplicateSets.filter(set => {
      if (confidenceFilter === 'all') return true
      if (confidenceFilter === 'high') return set.confidence >= 90
      if (confidenceFilter === 'medium') return set.confidence >= 70 && set.confidence < 90
      if (confidenceFilter === 'low') return set.confidence < 70
      return true
    })
  }

  const filteredSets = getFilteredSets()
  const nonExcludedCount = filteredSets.filter(s => !s.excluded).length

  return (
    <div className="min-h-screen bg-gray-50">
      <AppNav />
      <div className="max-w-6xl mx-auto py-8 px-4">
        {/* Header */}
        <div className="mb-6">
          <button
            onClick={() => router.push('/scan')}
            className="text-sm text-gray-500 hover:text-gray-700 mb-4"
          >
            &larr; Back to scans
          </button>
          <div className="flex justify-between items-start">
            <div>
              <h1 className="text-2xl font-bold text-gray-900">Review Duplicates</h1>
              <p className="text-gray-600 mt-1">
                Found {scan.duplicates_found} duplicate sets from {scan.records_scanned.toLocaleString()} {scan.object_type}
              </p>
            </div>
            <div className="text-right">
              <p className="text-sm text-gray-500">Scan completed</p>
              <p className="text-sm text-gray-500">
                {new Date(scan.created_at).toLocaleDateString()}
              </p>
            </div>
          </div>
        </div>

        {/* Filters and Actions Bar */}
        <div className="bg-white rounded-lg shadow p-4 mb-6 flex items-center justify-between">
          <div className="flex items-center gap-4">
            {/* Confidence Filter */}
            <div className="flex items-center gap-2">
              <label className="text-sm text-gray-500">Confidence:</label>
              <select
                value={confidenceFilter}
                onChange={(e) => setConfidenceFilter(e.target.value as ConfidenceFilter)}
                className="px-3 py-1.5 border border-gray-300 rounded-md text-sm"
              >
                <option value="all">All</option>
                <option value="high">High (90%+)</option>
                <option value="medium">Medium (70-90%)</option>
                <option value="low">Low (&lt;70%)</option>
              </select>
            </div>

            {/* Selection Actions */}
            <div className="flex items-center gap-2 border-l pl-4">
              <button
                onClick={selectAll}
                className="text-sm text-blue-600 hover:text-blue-700"
              >
                Select all ({nonExcludedCount})
              </button>
              <span className="text-gray-300">|</span>
              <button
                onClick={deselectAll}
                className="text-sm text-gray-500 hover:text-gray-700"
              >
                Deselect all
              </button>
            </div>
          </div>

          {/* Merge Error */}
          {mergeError && (
            <div className="text-sm text-red-600 mr-2">{mergeError}</div>
          )}

          {/* Export matches (works for any scan, incl. view-only) */}
          <button
            onClick={downloadMatches}
            className="px-4 py-2 border border-gray-300 text-gray-700 rounded-lg text-sm font-medium hover:bg-gray-50"
          >
            Export matches (Excel)
          </button>

          {/* Merge Button — disabled for view-only (dry-run) scans */}
          {scan.config?.dry_run ? (
            <span className="px-4 py-2 bg-amber-100 text-amber-800 rounded-lg text-sm font-medium">
              Dry-run — view only (no merge)
            </span>
          ) : (
            <button
              onClick={handleMerge}
              disabled={selectedSets.size === 0 || isMerging}
              className="px-6 py-2 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isMerging ? 'Starting merge...' : `Merge ${selectedSets.size} selected`}
            </button>
          )}
        </div>

        {/* Duplicate List */}
        {loadError ? (
          <div className="bg-white rounded-lg shadow p-8 text-center">
            <p className="text-red-600 mb-3">Couldn&apos;t load duplicates: {loadError}</p>
            <button
              onClick={fetchDuplicates}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
            >
              Retry
            </button>
          </div>
        ) : isLoading ? (
          <div className="bg-white rounded-lg shadow p-8 text-center text-gray-500">
            Loading duplicates...
          </div>
        ) : filteredSets.length === 0 ? (
          <div className="bg-white rounded-lg shadow p-8 text-center text-gray-500">
            No duplicates match the current filter.
          </div>
        ) : (
          <div className="space-y-4">
            {filteredSets.map(set => (
              <DuplicateCard
                key={set.id}
                duplicateSet={set}
                isSelected={selectedSets.has(set.id)}
                onToggleSelect={() => toggleSelection(set.id)}
                onToggleExclude={(excluded) => toggleExclude(set.id, excluded)}
                onExpand={() => setExpandedSet(set)}
              />
            ))}
          </div>
        )}

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="mt-6 flex justify-center gap-2">
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page === 1}
              className="px-4 py-2 border rounded-md disabled:opacity-50"
            >
              Previous
            </button>
            <span className="px-4 py-2 text-gray-600">
              Page {page} of {totalPages}
            </span>
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page === totalPages}
              className="px-4 py-2 border rounded-md disabled:opacity-50"
            >
              Next
            </button>
          </div>
        )}

        {/* Detail Modal */}
        {expandedSet && (
          <DuplicateDetail
            duplicateSet={expandedSet}
            scanId={scan.id}
            onClose={() => setExpandedSet(null)}
            onPreviewUpdated={(setId, updated) => {
              // Merge the full saved row (winner_record_id / winner_data / loser_data
              // / merged_preview / …) into the background list so reopening reflects
              // the new winner. Keep the OPEN modal's structure stable (only its
              // preview) to avoid a jarring column reorder mid-session.
              setDuplicateSets(prev =>
                prev.map(s => s.id === setId ? { ...s, ...(updated as Partial<DuplicateSet>) } : s)
              )
              setExpandedSet(prev =>
                prev
                  ? { ...prev, merged_preview: (updated.merged_preview as Record<string, unknown>) ?? prev.merged_preview }
                  : null
              )
            }}
          />
        )}
      </div>
    </div>
  )
}
