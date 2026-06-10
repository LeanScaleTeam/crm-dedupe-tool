'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { createClient } from '@/lib/supabase/client'
import { apiFetch } from '@/lib/api'
import type { User } from '@supabase/supabase-js'

interface CrmConnection {
  id: string
  crm_type: string
  portal_id: string | null
  created_at: string
}

interface ConnectClientProps {
  user: User
  existingConnection: CrmConnection | null
  oauthError?: string
  oauthSuccess?: boolean
}

export default function ConnectClient({ user, existingConnection, oauthError, oauthSuccess }: ConnectClientProps) {
  const router = useRouter()
  const [isLoading, setIsLoading] = useState(false)

  const handleHubSpotConnect = () => {
    setIsLoading(true)
    const clientId = process.env.NEXT_PUBLIC_HUBSPOT_CLIENT_ID
    const redirectUri = `${window.location.origin}/api/hubspot/callback`
    const scopes = [
      'crm.objects.contacts.read',
      'crm.objects.contacts.write',
    ].join(' ')

    const authUrl = `https://app.hubspot.com/oauth/authorize?client_id=${clientId}&redirect_uri=${encodeURIComponent(redirectUri)}&scope=${encodeURIComponent(scopes)}`
    window.location.href = authUrl
  }

  const handleSalesforceConnect = () => {
    setIsLoading(true)
    const clientId = process.env.NEXT_PUBLIC_SALESFORCE_CLIENT_ID
    const redirectUri = `${window.location.origin}/api/salesforce/callback`
    // Salesforce OAuth scopes for CRM access
    const scopes = ['api', 'refresh_token', 'full'].join(' ')

    const authUrl = `https://login.salesforce.com/services/oauth2/authorize?response_type=code&client_id=${clientId}&redirect_uri=${encodeURIComponent(redirectUri)}&scope=${encodeURIComponent(scopes)}`
    window.location.href = authUrl
  }

  const handleDisconnect = async () => {
    if (!existingConnection) return
    if (!confirm('Are you sure you want to disconnect your CRM?')) return
    setIsLoading(true)

    try {
      await apiFetch(`/${existingConnection.crm_type}/disconnect`, {
        method: 'DELETE',
      })
    } catch (error) {
      console.error('Failed to disconnect:', error)
    }

    router.refresh()
    setIsLoading(false)
  }

  const handleLogout = async () => {
    const supabase = createClient()
    await supabase.auth.signOut()
    router.push('/login')
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-4xl mx-auto py-12 px-4">
        {/* Header */}
        <div className="flex justify-between items-center mb-8">
          <div>
            <h1 className="text-2xl font-bold text-gray-900">CRM Dedup Tool</h1>
            <p className="text-gray-600">{user.email}</p>
          </div>
          <button
            onClick={handleLogout}
            className="text-sm text-gray-500 hover:text-gray-700"
          >
            Sign out
          </button>
        </div>

        {/* OAuth Feedback */}
        {oauthError && (
          <div className="bg-red-50 border border-red-200 text-red-800 p-4 rounded-lg mb-6">
            <p className="font-medium">Connection failed</p>
            <p className="text-sm mt-1">
              {oauthError === 'token_exchange_failed'
                ? 'Failed to connect to your CRM. Please try again.'
                : oauthError}
            </p>
          </div>
        )}
        {oauthSuccess && !oauthError && (
          <div className="bg-green-50 border border-green-200 text-green-800 p-4 rounded-lg mb-6">
            <p className="font-medium">CRM connected successfully!</p>
          </div>
        )}

        {/* Connection Status */}
        <div className="bg-white rounded-lg shadow p-6 mb-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">
            CRM Connection
          </h2>

          {existingConnection ? (
            <div className="space-y-4">
              <div className="flex items-center justify-between p-4 bg-green-50 rounded-lg">
                <div className="flex items-center gap-3">
                  <div className={`w-10 h-10 ${existingConnection.crm_type === 'salesforce' ? 'bg-blue-600' : 'bg-orange-500'} rounded-lg flex items-center justify-center`}>
                    <span className="text-white font-bold">{existingConnection.crm_type === 'salesforce' ? 'SF' : 'HS'}</span>
                  </div>
                  <div>
                    <p className="font-medium text-gray-900">
                      {existingConnection.crm_type === 'salesforce' ? 'Salesforce' : 'HubSpot'} Connected
                    </p>
                    <p className="text-sm text-gray-500">
                      {existingConnection.crm_type === 'salesforce' ? 'Org' : 'Portal'} ID: {existingConnection.portal_id || 'N/A'}
                    </p>
                  </div>
                </div>
                <button
                  onClick={handleDisconnect}
                  disabled={isLoading}
                  className="text-sm text-red-600 hover:text-red-700"
                >
                  Disconnect
                </button>
              </div>

              <button
                onClick={() => router.push('/scan')}
                className="w-full py-3 px-4 bg-blue-600 text-white rounded-lg font-medium hover:bg-blue-700 transition-colors"
              >
                Start Deduplication Scan
              </button>
            </div>
          ) : (
            <div className="space-y-4">
              <p className="text-gray-600">
                Connect your CRM to start finding and merging duplicate records.
              </p>

              <button
                onClick={handleHubSpotConnect}
                disabled={isLoading}
                className="w-full flex items-center justify-center gap-3 p-4 border-2 border-gray-200 rounded-lg hover:border-orange-500 hover:bg-orange-50 transition-colors disabled:opacity-50"
              >
                <div className="w-10 h-10 bg-orange-500 rounded-lg flex items-center justify-center">
                  <span className="text-white font-bold">HS</span>
                </div>
                <div className="text-left">
                  <p className="font-medium text-gray-900">Connect HubSpot</p>
                  <p className="text-sm text-gray-500">
                    Authorize access to contacts, companies, and deals
                  </p>
                </div>
              </button>

              <button
                onClick={handleSalesforceConnect}
                disabled={isLoading}
                className="w-full flex items-center justify-center gap-3 p-4 border-2 border-gray-200 rounded-lg hover:border-blue-500 hover:bg-blue-50 transition-colors disabled:opacity-50"
              >
                <div className="w-10 h-10 bg-blue-600 rounded-lg flex items-center justify-center">
                  <span className="text-white font-bold">SF</span>
                </div>
                <div className="text-left">
                  <p className="font-medium text-gray-900">Connect Salesforce</p>
                  <p className="text-sm text-gray-500">
                    Authorize access to contacts, accounts, and opportunities
                  </p>
                </div>
              </button>
            </div>
          )}
        </div>

        {/* Info */}
        <div className="bg-blue-50 rounded-lg p-4">
          <h3 className="font-medium text-blue-900 mb-2">How it works</h3>
          <ol className="text-sm text-blue-800 space-y-1 list-decimal list-inside">
            <li>Connect your CRM with OAuth (secure, no password stored)</li>
            <li>Configure your deduplication rules</li>
            <li>Review detected duplicates with confidence scores</li>
            <li>Approve and execute bulk merges</li>
            <li>Download a report for your records</li>
          </ol>
        </div>
      </div>
    </div>
  )
}
