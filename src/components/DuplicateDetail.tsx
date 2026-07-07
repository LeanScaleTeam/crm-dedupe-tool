'use client'

import { useState, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

interface Contact {
  id?: string
  email?: string
  first_name?: string
  last_name?: string
  full_name?: string
  phone?: string
  company?: string
  job_title?: string
  created_at?: string
  updated_at?: string
  association_count?: number
  raw_properties?: Record<string, unknown>
}

interface DuplicateSet {
  id: string
  confidence: number
  winner_record_id: string
  loser_record_ids: string[]
  winner_data: Contact
  loser_data: Contact[]
  merged_preview: Record<string, unknown>
  excluded: boolean
  merged: boolean
  excluded_record_ids?: string[]
}

interface DuplicateDetailProps {
  duplicateSet: DuplicateSet
  scanId: string
  onClose: () => void
  onPreviewUpdated?: (setId: string, preview: Record<string, unknown>) => void
}

function getContactName(contact: Contact): string {
  if (contact.full_name) return contact.full_name
  const parts = [contact.first_name, contact.last_name].filter(Boolean)
  return parts.join(' ') || 'Unknown'
}

function formatDate(dateStr?: string): string {
  if (!dateStr) return '-'
  try {
    return new Date(dateStr).toLocaleDateString()
  } catch {
    return '-'
  }
}

// Editable fields the user can pick values for
const EDITABLE_FIELDS: { key: string; label: string }[] = [
  { key: 'email', label: 'Email' },
  { key: 'first_name', label: 'First Name' },
  { key: 'last_name', label: 'Last Name' },
  { key: 'phone', label: 'Phone' },
  { key: 'company', label: 'Company' },
  { key: 'job_title', label: 'Job Title' },
]

// Read-only metadata fields
const METADATA_FIELDS: { key: string; label: string; format?: 'date' | 'number' }[] = [
  { key: 'created_at', label: 'Created', format: 'date' },
  { key: 'updated_at', label: 'Updated', format: 'date' },
  { key: 'association_count', label: 'Associations', format: 'number' },
]

export default function DuplicateDetail({
  duplicateSet,
  scanId,
  onClose,
  onPreviewUpdated,
}: DuplicateDetailProps) {
  const winner = duplicateSet.winner_data
  const losers = duplicateSet.loser_data
  const allContacts = [winner, ...losers]

  // Initialize merged preview from the stored preview or build from winner
  const [mergedPreview, setMergedPreview] = useState<Record<string, unknown>>(() => {
    const preview = { ...duplicateSet.merged_preview }
    for (const { key } of EDITABLE_FIELDS) {
      if (preview[key] === undefined) {
        preview[key] = getFieldValue(winner, key) || ''
      }
    }
    if (!preview.created_at) preview.created_at = winner.created_at
    if (!preview.updated_at) preview.updated_at = winner.updated_at
    if (preview.association_count === undefined) preview.association_count = winner.association_count
    return preview
  })

  // Records the reviewer marked "not a duplicate" — excluded from the merge.
  const [excludedIds, setExcludedIds] = useState<Set<string>>(
    () => new Set(duplicateSet.excluded_record_ids || [])
  )

  const [isSaving, setIsSaving] = useState(false)
  const [hasChanges, setHasChanges] = useState(false)

  const pickFieldValue = useCallback((key: string, value: string) => {
    setMergedPreview(prev => ({ ...prev, [key]: value }))
    setHasChanges(true)
  }, [])

  const toggleExcluded = useCallback((recordId?: string) => {
    if (!recordId) return
    setExcludedIds(prev => {
      const next = new Set(prev)
      if (next.has(recordId)) next.delete(recordId)
      else next.add(recordId)
      return next
    })
    setHasChanges(true)
  }, [])

  const excludedCount = losers.filter(l => l.id && excludedIds.has(l.id)).length
  const mergingCount = allContacts.length - excludedCount // winner + kept losers

  const saveChanges = useCallback(async () => {
    setIsSaving(true)
    try {
      const response = await apiFetch(
        `/scan/${scanId}/duplicate-sets/${duplicateSet.id}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            merged_preview: mergedPreview,
            excluded_record_ids: Array.from(excludedIds),
          }),
        }
      )
      if (response.ok) {
        setHasChanges(false)
        onPreviewUpdated?.(duplicateSet.id, mergedPreview)
      }
    } catch (error) {
      console.error('Failed to save changes:', error)
    } finally {
      setIsSaving(false)
    }
  }, [scanId, duplicateSet.id, mergedPreview, excludedIds, onPreviewUpdated])

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black bg-opacity-50" onClick={onClose} />

      {/* Modal */}
      <div className="relative min-h-screen flex items-center justify-center p-4">
        <div className="relative bg-white rounded-xl shadow-xl max-w-5xl w-full max-h-[90vh] overflow-hidden">
          {/* Header */}
          <div className="sticky top-0 bg-white border-b px-6 py-4 flex items-center justify-between z-10">
            <div>
              <h2 className="text-xl font-semibold text-gray-900">
                Duplicate Set Details
              </h2>
              <p className="text-sm text-gray-500 mt-1">
                {allContacts.length} records · {duplicateSet.confidence.toFixed(0)}% match
                {excludedCount > 0 && (
                  <span className="text-amber-700">
                    {' '}· merging {mergingCount}, {excludedCount} excluded
                  </span>
                )}
              </p>
            </div>
            <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
              <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Content */}
          <div className="p-6 overflow-y-auto max-h-[calc(90vh-140px)]">
            {/* Instructions */}
            <div className="mb-4 p-3 bg-blue-50 rounded-lg text-sm text-blue-800">
              Click any field value to use it in the merged result. Use <strong>Not a duplicate</strong> on a
              record to exclude it from the merge (it stays untouched).
            </div>

            {/* Comparison Table */}
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b">
                    <th className="text-left py-3 px-4 text-sm font-medium text-gray-500 w-28">
                      Field
                    </th>
                    <th className="text-left py-3 px-4 text-sm font-medium text-green-700 bg-green-50">
                      <div className="flex items-center gap-2">
                        <span className="w-6 h-6 bg-green-500 text-white rounded-full flex items-center justify-center text-xs font-bold">W</span>
                        {getContactName(winner)}
                        <span className="text-xs text-green-500">(Winner)</span>
                      </div>
                    </th>
                    {losers.map((loser, idx) => {
                      const isExcluded = !!(loser.id && excludedIds.has(loser.id))
                      return (
                        <th key={idx} className={`text-left py-3 px-4 text-sm font-medium bg-gray-50 ${isExcluded ? 'opacity-50' : 'text-gray-600'}`}>
                          <div className="flex items-center gap-2">
                            <span className="w-6 h-6 bg-gray-400 text-white rounded-full flex items-center justify-center text-xs font-bold">L</span>
                            <span className={isExcluded ? 'line-through text-gray-500' : ''}>{getContactName(loser)}</span>
                          </div>
                          <button
                            onClick={() => toggleExcluded(loser.id)}
                            className={`mt-1 text-xs font-medium ${isExcluded ? 'text-blue-600 hover:text-blue-700' : 'text-red-500 hover:text-red-600'}`}
                          >
                            {isExcluded ? '↺ Include' : '✕ Not a duplicate'}
                          </button>
                        </th>
                      )
                    })}
                    <th className="text-left py-3 px-4 text-sm font-medium text-blue-700 bg-blue-50">
                      <div className="flex items-center gap-2">
                        <span className="w-6 h-6 bg-blue-500 text-white rounded-full flex items-center justify-center text-xs font-bold">M</span>
                        Merged Result
                      </div>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {/* Editable fields */}
                  {EDITABLE_FIELDS.map(({ key, label }) => {
                    const mergedVal = String(mergedPreview[key] || '')
                    return (
                      <tr key={key} className="border-b">
                        <td className="py-3 px-4 text-sm font-medium text-gray-500">{label}</td>
                        {allContacts.map((contact, idx) => {
                          const val = getFieldValue(contact, key)
                          const isSelected = val && val === mergedVal
                          const isEmpty = !val
                          const colExcluded = idx > 0 && !!contact.id && excludedIds.has(contact.id)
                          return (
                            <td
                              key={idx}
                              className={`py-3 px-4 text-sm transition-colors ${
                                idx === 0 ? 'bg-green-50' : 'bg-gray-50'
                              } ${colExcluded ? 'opacity-40 cursor-not-allowed line-through' : 'cursor-pointer'} ${
                                isSelected && !colExcluded
                                  ? 'ring-2 ring-inset ring-blue-500 font-medium text-blue-900'
                                  : isEmpty
                                  ? 'text-gray-300'
                                  : `text-gray-900 ${colExcluded ? '' : 'hover:bg-blue-50'}`
                              }`}
                              onClick={() => {
                                if (val && !colExcluded) pickFieldValue(key, val)
                              }}
                              title={colExcluded ? 'Excluded record' : val ? 'Click to use this value' : ''}
                            >
                              {val || '-'}
                              {isSelected && !colExcluded && (
                                <span className="ml-1.5 text-blue-500 text-xs">&#10003;</span>
                              )}
                            </td>
                          )
                        })}
                        <td className="py-3 px-4 text-sm font-medium text-gray-900 bg-blue-50">
                          {mergedVal || '-'}
                        </td>
                      </tr>
                    )
                  })}

                  {/* Separator */}
                  <tr>
                    <td colSpan={allContacts.length + 2} className="py-2">
                      <div className="text-xs text-gray-400 uppercase tracking-wider px-4">Metadata (from winner)</div>
                    </td>
                  </tr>

                  {/* Metadata fields (read-only) */}
                  {METADATA_FIELDS.map(({ key, label, format }) => (
                    <tr key={key} className="border-b">
                      <td className="py-3 px-4 text-sm font-medium text-gray-500">{label}</td>
                      {allContacts.map((contact, idx) => {
                        const raw = getFieldValue(contact, key)
                        const display = format === 'date' ? formatDate(raw) : (raw || '-')
                        const colExcluded = idx > 0 && !!contact.id && excludedIds.has(contact.id)
                        return (
                          <td key={idx} className={`py-3 px-4 text-sm text-gray-600 ${idx === 0 ? 'bg-green-50' : 'bg-gray-50'} ${colExcluded ? 'opacity-40 line-through' : ''}`}>
                            {display}
                          </td>
                        )
                      })}
                      <td className="py-3 px-4 text-sm text-gray-600 bg-blue-50">
                        {format === 'date'
                          ? formatDate(mergedPreview[key] as string)
                          : String(mergedPreview[key] ?? '-')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Legend */}
            <div className="mt-6 p-4 bg-gray-50 rounded-lg">
              <h3 className="text-sm font-medium text-gray-700 mb-2">How it works</h3>
              <ul className="text-sm text-gray-600 space-y-1">
                <li className="flex items-center gap-2">
                  <span className="inline-block w-4 h-4 ring-2 ring-blue-500 rounded"></span>
                  Click any cell to select that value for the merged result
                </li>
                <li className="flex items-center gap-2">
                  <span className="w-3 h-3 bg-green-500 rounded-full"></span>
                  Winner record (selected by your merge rules)
                </li>
                <li className="flex items-center gap-2">
                  <span className="text-red-500 text-xs font-bold">✕</span>
                  <strong>Not a duplicate</strong> — excludes that record; it won&apos;t be merged and stays untouched
                </li>
                <li className="flex items-center gap-2">
                  <span className="w-3 h-3 bg-blue-500 rounded-full"></span>
                  Final merged result — what will be written to your CRM
                </li>
              </ul>
            </div>
          </div>

          {/* Footer */}
          <div className="sticky bottom-0 bg-white border-t px-6 py-4 flex justify-between items-center">
            <div className="text-sm text-gray-500">
              {hasChanges && 'You have unsaved changes'}
            </div>
            <div className="flex gap-3">
              <button onClick={onClose} className="px-4 py-2 text-gray-600 hover:text-gray-800">
                Close
              </button>
              {hasChanges && (
                <button
                  onClick={saveChanges}
                  disabled={isSaving}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50"
                >
                  {isSaving ? 'Saving...' : 'Save Changes'}
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

/** Get a field value from a Contact using model field names */
function getFieldValue(contact: Contact, key: string): string {
  const value = contact[key as keyof Contact]
  if (value === undefined || value === null) return ''
  return String(value)
}
