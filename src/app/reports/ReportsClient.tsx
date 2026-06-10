'use client'

import { useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { apiFetch } from '@/lib/api'

interface Report {
  id: string
  merge_id: string
  created_at: string
  report_data: {
    generated_at: string
    crm_type: string
    portal_id: string
    scan: {
      object_type: string
      records_scanned: number
      duplicates_found: number
    }
    merge: {
      completed_sets: number
      failed_sets: number
      success_rate: number
    }
  }
}

interface ReportsClientProps {
  userId: string
}

export default function ReportsClient({}: ReportsClientProps) {
  const router = useRouter()
  const [reports, setReports] = useState<Report[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)

  useEffect(() => {
    fetchReports()
  }, [page])

  const fetchReports = async () => {
    setIsLoading(true)
    try {
      const response = await apiFetch(`/reports/mine?page=${page}`)

      if (response.ok) {
        const data = await response.json()
        setReports(data.reports)
        setTotalPages(data.total_pages)
      }
    } catch (error) {
      console.error('Failed to fetch reports:', error)
    } finally {
      setIsLoading(false)
    }
  }

  const downloadPdf = async (reportId: string) => {
    try {
      const response = await apiFetch(`/reports/${reportId}/pdf`)

      if (response.ok) {
        const blob = await response.blob()
        const url = window.URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = `dedup-report-${reportId.slice(0, 8)}.pdf`
        document.body.appendChild(a)
        a.click()
        a.remove()
        window.URL.revokeObjectURL(url)
      }
    } catch (error) {
      console.error('Failed to download PDF:', error)
    }
  }

  const formatDate = (dateStr: string) => {
    try {
      return new Date(dateStr).toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      })
    } catch {
      return dateStr
    }
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-4xl mx-auto py-12 px-4">
        {/* Header */}
        <div className="mb-8">
          <button
            onClick={() => router.push('/connect')}
            className="text-sm text-gray-500 hover:text-gray-700 mb-4"
          >
            &larr; Back to dashboard
          </button>
          <h1 className="text-2xl font-bold text-gray-900">Reports</h1>
          <p className="text-gray-600 mt-1">
            Download and review your deduplication reports
          </p>
        </div>

        {/* Reports List */}
        {isLoading ? (
          <div className="bg-white rounded-lg shadow p-8 text-center text-gray-500">
            Loading reports...
          </div>
        ) : reports.length === 0 ? (
          <div className="bg-white rounded-lg shadow p-8 text-center">
            <div className="text-gray-400 mb-4">
              <svg className="w-16 h-16 mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1} d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
              </svg>
            </div>
            <h3 className="text-lg font-medium text-gray-900 mb-2">No reports yet</h3>
            <p className="text-gray-500 mb-4">
              Reports are generated after you complete a merge operation.
            </p>
            <button
              onClick={() => router.push('/scan')}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
            >
              Start a Scan
            </button>
          </div>
        ) : (
          <div className="space-y-4">
            {reports.map(report => (
              <div
                key={report.id}
                className="bg-white rounded-lg shadow p-6"
              >
                <div className="flex items-center justify-between">
                  <div>
                    <h3 className="font-medium text-gray-900">
                      {report.report_data.crm_type.charAt(0).toUpperCase() + report.report_data.crm_type.slice(1)} {report.report_data.scan.object_type.charAt(0).toUpperCase() + report.report_data.scan.object_type.slice(1)} Deduplication
                    </h3>
                    <p className="text-sm text-gray-500">
                      {formatDate(report.created_at)}
                    </p>
                  </div>

                  <button
                    onClick={() => downloadPdf(report.id)}
                    className="flex items-center gap-2 px-4 py-2 border border-gray-300 rounded-lg hover:bg-gray-50"
                  >
                    <svg className="w-5 h-5 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                    </svg>
                    Download PDF
                  </button>
                </div>

                {/* Stats */}
                <div className="mt-4 grid grid-cols-4 gap-4">
                  <div className="text-center">
                    <p className="text-2xl font-semibold text-gray-900">
                      {report.report_data.scan.records_scanned.toLocaleString()}
                    </p>
                    <p className="text-xs text-gray-500">Records Scanned</p>
                  </div>
                  <div className="text-center">
                    <p className="text-2xl font-semibold text-gray-900">
                      {report.report_data.scan.duplicates_found.toLocaleString()}
                    </p>
                    <p className="text-xs text-gray-500">Duplicates Found</p>
                  </div>
                  <div className="text-center">
                    <p className="text-2xl font-semibold text-green-600">
                      {report.report_data.merge.completed_sets.toLocaleString()}
                    </p>
                    <p className="text-xs text-gray-500">Merged</p>
                  </div>
                  <div className="text-center">
                    <p className="text-2xl font-semibold text-gray-900">
                      {report.report_data.merge.success_rate}%
                    </p>
                    <p className="text-xs text-gray-500">Success Rate</p>
                  </div>
                </div>
              </div>
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
      </div>
    </div>
  )
}
