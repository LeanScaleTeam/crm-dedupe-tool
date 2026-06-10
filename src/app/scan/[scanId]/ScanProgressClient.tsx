'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { apiFetch } from '@/lib/api'

interface Scan {
  id: string
  object_type: string
  status: string
  progress: number
  records_scanned: number
  duplicates_found: number
  error_message: string | null
  created_at: string
}

interface ScanProgressClientProps {
  scan: Scan
}

export default function ScanProgressClient({ scan: initialScan }: ScanProgressClientProps) {
  const router = useRouter()
  const [scan, setScan] = useState(initialScan)
  const [isPolling, setIsPolling] = useState(true)

  useEffect(() => {
    if (!isPolling) return
    if (scan.status === 'completed' || scan.status === 'failed' || scan.status === 'cancelled') {
      setIsPolling(false)
      if (scan.status === 'completed') {
        router.push(`/review/${scan.id}`)
      }
      return
    }

    const pollStatus = async () => {
      try {
        const response = await apiFetch(`/scan/${scan.id}/status`)

        if (response.ok) {
          const data = await response.json()
          setScan(prev => ({
            ...prev,
            status: data.status,
            progress: data.progress,
            records_scanned: data.records_scanned,
            duplicates_found: data.duplicates_found,
            error_message: data.error_message,
          }))
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

    const interval = setInterval(pollStatus, 2000)
    return () => clearInterval(interval)
  }, [isPolling, scan.id, scan.status, router])

  const handleCancel = async () => {
    // TODO: Implement cancel scan
    router.push('/scan')
  }

  const getStatusMessage = () => {
    switch (scan.status) {
      case 'pending':
        return 'Initializing scan...'
      case 'running':
        return `Scanning ${scan.object_type}...`
      case 'completed':
        return 'Scan complete!'
      case 'failed':
        return 'Scan failed'
      case 'cancelled':
        return 'Scan cancelled'
      default:
        return 'Processing...'
    }
  }

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center">
      <div className="max-w-md w-full mx-4">
        <div className="bg-white rounded-lg shadow-lg p-8 text-center">
          {/* Status Icon */}
          <div className="mb-6">
            {scan.status === 'running' || scan.status === 'pending' ? (
              <div className="w-20 h-20 mx-auto border-4 border-blue-200 border-t-blue-600 rounded-full animate-spin" />
            ) : scan.status === 'completed' ? (
              <div className="w-20 h-20 mx-auto bg-green-100 rounded-full flex items-center justify-center">
                <svg className="w-10 h-10 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
              </div>
            ) : scan.status === 'failed' ? (
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
          {(scan.status === 'running' || scan.status === 'pending') && (
            <div className="mb-4">
              <div className="w-full bg-gray-200 rounded-full h-2">
                <div
                  className="bg-blue-600 h-2 rounded-full transition-all duration-500"
                  style={{ width: `${scan.progress}%` }}
                />
              </div>
              <p className="text-sm text-gray-500 mt-2">{scan.progress}% complete</p>
            </div>
          )}

          {/* Stats */}
          <div className="grid grid-cols-2 gap-4 mb-6 text-left">
            <div className="bg-gray-50 rounded-lg p-3">
              <p className="text-sm text-gray-500">Records Scanned</p>
              <p className="text-2xl font-semibold text-gray-900">
                {scan.records_scanned.toLocaleString()}
              </p>
            </div>
            <div className="bg-gray-50 rounded-lg p-3">
              <p className="text-sm text-gray-500">Duplicates Found</p>
              <p className="text-2xl font-semibold text-blue-600">
                {scan.duplicates_found.toLocaleString()}
              </p>
            </div>
          </div>

          {/* Error Message */}
          {scan.error_message && (
            <div className="bg-red-50 text-red-800 p-4 rounded-lg mb-4 text-left">
              <p className="font-medium">Error</p>
              <p className="text-sm">{scan.error_message}</p>
            </div>
          )}

          {/* Actions */}
          {(scan.status === 'running' || scan.status === 'pending') && (
            <button
              onClick={handleCancel}
              className="text-gray-500 hover:text-gray-700 text-sm"
            >
              Cancel Scan
            </button>
          )}

          {scan.status === 'failed' && (
            <button
              onClick={() => router.push('/scan')}
              className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700"
            >
              Try Again
            </button>
          )}

          {scan.status === 'completed' && (
            <button
              onClick={() => router.push(`/review/${scan.id}`)}
              className="bg-blue-600 text-white px-6 py-2 rounded-lg hover:bg-blue-700"
            >
              Review Duplicates
            </button>
          )}
        </div>

        {/* Help Text */}
        {(scan.status === 'running' || scan.status === 'pending') && (
          <p className="text-center text-sm text-gray-500 mt-4">
            You can leave this page - we&apos;ll keep scanning in the background.
          </p>
        )}
      </div>
    </div>
  )
}
