'use client'

import { useState, useEffect, use } from 'react'
import { useRouter } from 'next/navigation'
import { apiFetch } from '@/lib/api'

interface MergePageProps {
  params: Promise<{ mergeId: string }>
}

interface MergeStatus {
  id: string
  status: string
  total_sets: number
  completed_sets: number
  failed_sets: number
  error_log: { set_id: string; error: string }[] | null
  started_at: string | null
  completed_at: string | null
}

export default function MergePage({ params }: MergePageProps) {
  const { mergeId } = use(params)
  const router = useRouter()
  const [merge, setMerge] = useState<MergeStatus | null>(null)
  const [isPolling, setIsPolling] = useState(true)
  const [isGeneratingReport, setIsGeneratingReport] = useState(false)
  const [reportError, setReportError] = useState<string | null>(null)

  useEffect(() => {
    if (!isPolling) return

    const pollStatus = async () => {
      try {
        const response = await apiFetch(`/merge/${mergeId}/status`)

        if (response.ok) {
          const data = await response.json()
          setMerge(data)

          if (data.status === 'completed' || data.status === 'failed') {
            setIsPolling(false)
          }
        }
      } catch (error) {
        console.error('Failed to poll status:', error)
        // Session dropped — stop the silent retry loop and re-auth.
        if (error instanceof Error && error.message.includes('Not authenticated')) {
          setIsPolling(false)
          router.push('/login')
        }
      }
    }

    pollStatus()
    const interval = setInterval(pollStatus, 2000)
    return () => clearInterval(interval)
  }, [isPolling, mergeId, router])

  const getProgress = () => {
    if (!merge || merge.total_sets === 0) return 0
    return Math.round((merge.completed_sets / merge.total_sets) * 100)
  }

  const getStatusMessage = () => {
    if (!merge) return 'Loading...'
    switch (merge.status) {
      case 'pending':
        return 'Preparing merge...'
      case 'running':
        return `Merging duplicates... ${merge.completed_sets} of ${merge.total_sets}`
      case 'completed':
        return 'Merge complete!'
      case 'failed':
        return 'Merge failed'
      case 'paused':
        return 'Merge paused'
      default:
        return 'Processing...'
    }
  }

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center">
      <div className="max-w-lg w-full mx-4">
        <div className="bg-white rounded-lg shadow-lg p-8 text-center">
          {/* Status Icon */}
          <div className="mb-6">
            {(!merge || merge.status === 'running' || merge.status === 'pending') ? (
              <div className="w-20 h-20 mx-auto border-4 border-blue-200 border-t-blue-600 rounded-full animate-spin" />
            ) : merge.status === 'completed' ? (
              <div className="w-20 h-20 mx-auto bg-green-100 rounded-full flex items-center justify-center">
                <svg className="w-10 h-10 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
              </div>
            ) : merge.status === 'failed' ? (
              <div className="w-20 h-20 mx-auto bg-red-100 rounded-full flex items-center justify-center">
                <svg className="w-10 h-10 text-red-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </div>
            ) : null}
          </div>

          {/* Status Message */}
          <h2 className="text-xl font-semibold text-gray-900 mb-2">
            {getStatusMessage()}
          </h2>

          {/* Progress Bar */}
          {merge && (merge.status === 'running' || merge.status === 'pending') && (
            <div className="mb-6">
              <div className="w-full bg-gray-200 rounded-full h-2">
                <div
                  className="bg-blue-600 h-2 rounded-full transition-all duration-500"
                  style={{ width: `${getProgress()}%` }}
                />
              </div>
              <p className="text-sm text-gray-500 mt-2">{getProgress()}% complete</p>
            </div>
          )}

          {/* Stats */}
          {merge && (
            <div className="grid grid-cols-3 gap-4 mb-6 text-left">
              <div className="bg-gray-50 rounded-lg p-3">
                <p className="text-sm text-gray-500">Total</p>
                <p className="text-2xl font-semibold text-gray-900">
                  {merge.total_sets}
                </p>
              </div>
              <div className="bg-green-50 rounded-lg p-3">
                <p className="text-sm text-gray-500">Merged</p>
                <p className="text-2xl font-semibold text-green-600">
                  {merge.completed_sets}
                </p>
              </div>
              <div className="bg-red-50 rounded-lg p-3">
                <p className="text-sm text-gray-500">Failed</p>
                <p className="text-2xl font-semibold text-red-600">
                  {merge.failed_sets}
                </p>
              </div>
            </div>
          )}

          {/* Error Log */}
          {merge?.error_log && merge.error_log.length > 0 && (
            <div className="bg-red-50 rounded-lg p-4 mb-6 text-left">
              <p className="font-medium text-red-900 mb-2">
                {merge.error_log.length} errors occurred:
              </p>
              <ul className="text-sm text-red-800 space-y-1 max-h-32 overflow-y-auto">
                {merge.error_log.slice(0, 5).map((err, idx) => (
                  <li key={idx}>{err.error}</li>
                ))}
                {merge.error_log.length > 5 && (
                  <li className="text-red-600">
                    +{merge.error_log.length - 5} more errors
                  </li>
                )}
              </ul>
            </div>
          )}

          {/* Completion Actions */}
          {merge?.status === 'completed' && (
            <div className="space-y-3">
              <button
                onClick={() => router.push('/reports')}
                className="w-full py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700"
              >
                View Reports
              </button>
              <button
                onClick={async () => {
                  setIsGeneratingReport(true)
                  setReportError(null)
                  try {
                    const response = await apiFetch(
                      `/reports/generate/${mergeId}`,
                      { method: 'POST' }
                    )
                    if (response.ok) {
                      router.push('/reports')
                    } else {
                      const data = await response.json().catch(() => ({}))
                      setReportError(data.detail || 'Failed to generate report')
                    }
                  } catch {
                    setReportError('Failed to generate report')
                  } finally {
                    setIsGeneratingReport(false)
                  }
                }}
                disabled={isGeneratingReport}
                className="w-full py-3 border border-gray-300 text-gray-700 rounded-lg font-medium hover:bg-gray-50 disabled:opacity-50"
              >
                {isGeneratingReport ? 'Generating...' : 'Regenerate Report'}
              </button>
              {reportError && (
                <p className="text-sm text-red-600">{reportError}</p>
              )}
              <button
                onClick={() => router.push('/scan')}
                className="w-full py-3 border border-gray-300 text-gray-700 rounded-lg font-medium hover:bg-gray-50"
              >
                Start New Scan
              </button>
            </div>
          )}

          {merge?.status === 'failed' && (
            <button
              onClick={() => router.push('/scan')}
              className="w-full py-3 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700"
            >
              Try Again
            </button>
          )}
        </div>

        {/* Help Text */}
        {merge && (merge.status === 'running' || merge.status === 'pending') && (
          <p className="text-center text-sm text-gray-500 mt-4">
            This may take a few minutes for large datasets.
            <br />
            Do not close this page until complete.
          </p>
        )}
      </div>
    </div>
  )
}
