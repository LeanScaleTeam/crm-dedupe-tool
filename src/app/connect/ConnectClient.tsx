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
  org_id?: string | null
  created_at: string
}

interface ConnectClientProps {
  user: User
  connections: CrmConnection[]
  oauthError?: string
  oauthSuccess?: boolean
}

type SfEnv = 'sandbox' | 'production'

function parseOrg(portalId: string | null): { orgId: string; instance: string | null } {
  const p = portalId || ''
  if (p.includes('|')) {
    const [orgId, instance] = p.split('|')
    return { orgId, instance }
  }
  return { orgId: p, instance: null }
}

export default function ConnectClient({ user, connections, oauthError, oauthSuccess }: ConnectClientProps) {
  const router = useRouter()
  const [isLoading, setIsLoading] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [sfEnv, setSfEnv] = useState<SfEnv>('sandbox')

  const handleSalesforceConnect = async () => {
    setIsLoading(true)
    const clientId = process.env.NEXT_PUBLIC_SALESFORCE_CLIENT_ID
    const redirectUri = `${window.location.origin}/api/salesforce/callback`
    const scopes = ['api', 'refresh_token', 'full'].join(' ')

    // Multi-org: pick the OAuth host per connect. Production vs sandbox gateway
    // (both route to the org's My Domain after login), so a user can attach any org.
    const loginBase =
      sfEnv === 'production' ? 'https://login.salesforce.com' : 'https://test.salesforce.com'

    // PKCE (required by External Client Apps): verifier -> cookie, S256 challenge on the URL.
    const b64url = (bytes: Uint8Array) =>
      btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    const verifier = b64url(crypto.getRandomValues(new Uint8Array(64)))
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier))
    const challenge = b64url(new Uint8Array(digest))
    document.cookie = `sf_pkce_verifier=${verifier}; path=/; max-age=600; samesite=lax`
    // Remember which host we used so the callback exchanges the code at the same host.
    document.cookie = `sf_login_host=${encodeURIComponent(loginBase)}; path=/; max-age=600; samesite=lax`

    const authUrl = `${loginBase}/services/oauth2/authorize?response_type=code&client_id=${clientId}`
      + `&redirect_uri=${encodeURIComponent(redirectUri)}&scope=${encodeURIComponent(scopes)}`
      + `&code_challenge=${challenge}&code_challenge_method=S256`
    window.location.href = authUrl
  }

  const handleDisconnect = async (conn: CrmConnection) => {
    if (!confirm('Disconnect this org? Its stored tokens will be removed.')) return
    setBusyId(conn.id)
    try {
      if (conn.crm_type === 'salesforce') {
        await apiFetch(`/salesforce/connections/${conn.id}`, { method: 'DELETE' })
      } else {
        await apiFetch(`/${conn.crm_type}/disconnect`, { method: 'DELETE' })
      }
    } catch (error) {
      console.error('Failed to disconnect:', error)
    }
    setBusyId(null)
    router.refresh()
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
          <div className="flex items-center gap-4">
            <button
              onClick={() => router.push('/reports')}
              className="text-sm text-blue-600 hover:text-blue-700 font-medium"
            >
              Reports
            </button>
            <button onClick={handleLogout} className="text-sm text-gray-500 hover:text-gray-700">
              Sign out
            </button>
          </div>
        </div>

        {/* OAuth Feedback */}
        {oauthError && (
          <div className="bg-red-50 border border-red-200 text-red-800 p-4 rounded-lg mb-6">
            <p className="font-medium">Connection failed</p>
            <p className="text-sm mt-1 break-words">
              {oauthError === 'token_exchange_failed'
                ? 'Failed to connect to your CRM. Please try again.'
                : oauthError}
            </p>
          </div>
        )}
        {oauthSuccess && !oauthError && (
          <div className="bg-green-50 border border-green-200 text-green-800 p-4 rounded-lg mb-6">
            <p className="font-medium">Org connected successfully!</p>
          </div>
        )}

        {/* Connected orgs */}
        <div className="bg-white rounded-lg shadow p-6 mb-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">
            Connected orgs {connections.length > 0 && <span className="text-gray-400">({connections.length})</span>}
          </h2>

          {connections.length === 0 ? (
            <p className="text-gray-600">No orgs connected yet. Connect one below to start finding duplicates.</p>
          ) : (
            <div className="space-y-3">
              {connections.map((conn) => {
                const { orgId, instance } = parseOrg(conn.portal_id)
                const isSf = conn.crm_type === 'salesforce'
                return (
                  <div key={conn.id} className="flex items-center justify-between p-4 bg-gray-50 rounded-lg">
                    <div className="flex items-center gap-3 min-w-0">
                      <div className={`w-10 h-10 shrink-0 ${isSf ? 'bg-blue-600' : 'bg-orange-500'} rounded-lg flex items-center justify-center`}>
                        <span className="text-white font-bold">{isSf ? 'SF' : 'HS'}</span>
                      </div>
                      <div className="min-w-0">
                        <p className="font-medium text-gray-900">
                          {isSf ? 'Salesforce' : 'HubSpot'}
                          {instance?.includes('sandbox') && <span className="ml-2 text-xs px-2 py-0.5 rounded-full bg-amber-100 text-amber-800">Sandbox</span>}
                        </p>
                        <p className="text-sm text-gray-500 truncate">
                          {isSf ? `Org ${orgId}` : `Portal ${orgId}`}{instance ? ` · ${instance.replace('https://', '')}` : ''}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-3 shrink-0">
                      <button
                        onClick={() => router.push(`/scan?connection=${conn.id}`)}
                        className="py-2 px-4 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
                      >
                        Open
                      </button>
                      <button
                        onClick={() => handleDisconnect(conn)}
                        disabled={busyId === conn.id}
                        className="text-sm text-red-600 hover:text-red-700 disabled:opacity-50"
                      >
                        {busyId === conn.id ? '…' : 'Disconnect'}
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {/* Connect another org */}
        <div className="bg-white rounded-lg shadow p-6">
          <h2 className="text-lg font-semibold text-gray-900 mb-4">Connect another org</h2>

          {/* Environment toggle */}
          <div className="mb-4">
            <p className="text-sm text-gray-500 mb-2">Salesforce environment</p>
            <div className="inline-flex rounded-lg border border-gray-200 p-1">
              {(['sandbox', 'production'] as SfEnv[]).map((env) => (
                <button
                  key={env}
                  onClick={() => setSfEnv(env)}
                  className={`px-4 py-1.5 text-sm rounded-md capitalize transition-colors ${
                    sfEnv === env ? 'bg-blue-600 text-white' : 'text-gray-600 hover:text-gray-900'
                  }`}
                >
                  {env}
                </button>
              ))}
            </div>
          </div>

          <button
            onClick={handleSalesforceConnect}
            disabled={isLoading}
            className="w-full flex items-center justify-center gap-3 p-4 border-2 border-gray-200 rounded-lg hover:border-blue-500 hover:bg-blue-50 transition-colors disabled:opacity-50"
          >
            <div className="w-10 h-10 bg-blue-600 rounded-lg flex items-center justify-center">
              <span className="text-white font-bold">SF</span>
            </div>
            <div className="text-left">
              <p className="font-medium text-gray-900">Connect Salesforce ({sfEnv})</p>
              <p className="text-sm text-gray-500">Authorize access to a {sfEnv} org — you can add as many as you like</p>
            </div>
          </button>
        </div>

        {/* Info */}
        <div className="bg-blue-50 rounded-lg p-4 mt-6">
          <h3 className="font-medium text-blue-900 mb-2">How it works</h3>
          <ol className="text-sm text-blue-800 space-y-1 list-decimal list-inside">
            <li>Connect one or more CRM orgs with OAuth (secure, no password stored)</li>
            <li>Configure your deduplication rules</li>
            <li>Review detected duplicates with confidence scores</li>
            <li>Approve and execute bulk merges</li>
          </ol>
        </div>
      </div>
    </div>
  )
}
