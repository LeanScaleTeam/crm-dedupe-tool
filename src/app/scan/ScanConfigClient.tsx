'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { apiFetch } from '@/lib/api'
import AppNav from '@/components/AppNav'

interface CrmConnection {
  id: string
  crm_type: string
  portal_id: string | null
}

interface ScanConfigClientProps {
  userId: string
  connection: CrmConnection
}

type ObjectType = 'contacts' | 'accounts' | 'companies' | 'deals' | 'leads' | 'lead_conversion'
type WinnerRuleType = 'oldest_created' | 'most_recent' | 'most_associations' | 'custom_field' | 'none'

interface WinnerRule {
  type: WinnerRuleType
  customField?: string
  customValue?: string
}

// Object types are CRM-specific: HubSpot shows only Contacts + Companies (both
// real-merge). Salesforce shows Contacts + Accounts (config-driven dry-run), with
// Deals coming soon.
const objectTypesForCrm = (
  crmType: string,
): { value: ObjectType; label: string; description: string; available: boolean }[] => {
  if (crmType === 'hubspot') {
    return [
      { value: 'contacts', label: 'Contacts', description: 'People records in your CRM', available: true },
      { value: 'companies', label: 'Companies', description: 'Organization records — matched & merged by domain and name', available: true },
    ]
  }
  return [
    { value: 'contacts', label: 'Contacts', description: 'People records in your CRM', available: true },
    { value: 'leads', label: 'Leads', description: 'Lead records — matched & merged by email and name', available: true },
    { value: 'accounts', label: 'Accounts', description: 'Organization records — config-driven match, view-only (dry-run)', available: true },
    { value: 'lead_conversion', label: 'Leads → Contacts (convert)', description: 'Match leads to existing contacts and convert each lead into its matched contact', available: true },
    { value: 'deals', label: 'Deals', description: 'Sales pipeline records', available: false },
  ]
}

// Human label for an object type (used in the "what happens next" copy).
const OBJECT_LABEL: Record<ObjectType, string> = {
  contacts: 'contacts',
  leads: 'leads',
  accounts: 'accounts',
  companies: 'companies',
  deals: 'deals',
  lead_conversion: 'leads',
}

const ACCOUNT_PROFILES: { value: string; label: string }[] = [
  { value: 'scandit/account_v3', label: 'Scandit — Name + Domain + Country, Vertical discriminator (V3)' },
  { value: 'scandit/account_v2', label: 'Scandit — Name + Domain + Country (V2 baseline)' },
]

const WINNER_RULES: { value: WinnerRuleType; label: string; description: string }[] = [
  { value: 'oldest_created', label: 'Oldest Created', description: 'Record created first wins' },
  { value: 'most_recent', label: 'Most Recently Updated', description: 'Most actively maintained record wins' },
  { value: 'most_associations', label: 'Most Associated Records', description: 'Record with most deals/activities wins' },
  { value: 'custom_field', label: 'Custom Field Value', description: 'Specific field value determines winner' },
  { value: 'none', label: 'None', description: 'Skip this priority level' },
]

export default function ScanConfigClient({ connection }: ScanConfigClientProps) {
  const router = useRouter()
  const objectTypes = objectTypesForCrm(connection.crm_type)
  const [objectType, setObjectType] = useState<ObjectType>('contacts')
  const [matchProfile, setMatchProfile] = useState<string>('scandit/account_v3')
  const [accountEngine, setAccountEngine] = useState<'simple' | 'config'>('simple')
  const [configDryRun, setConfigDryRun] = useState(true)
  const [winnerRules, setWinnerRules] = useState<WinnerRule[]>([
    { type: 'oldest_created' },
    { type: 'most_associations' },
    { type: 'none' },
  ])
  const [confidenceThreshold, setConfidenceThreshold] = useState(90)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const updateRule = (index: number, rule: WinnerRule) => {
    const newRules = [...winnerRules]
    newRules[index] = rule
    setWinnerRules(newRules)
  }

  const handleStartScan = async () => {
    setIsLoading(true)
    setError(null)

    try {
      // Filter out 'none' rules
      const activeRules = winnerRules
        .filter(r => r.type !== 'none')
        .map(r => ({
          rule_type: r.type,
          field_name: r.customField || null,
          field_value: r.customValue || null,
        }))

      const response = await apiFetch(`/scan/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          connection_id: connection.id,
          config: {
            object_type: objectType,
            winner_rules: activeRules,
            confidence_threshold: confidenceThreshold / 100,
            ...(objectType === 'accounts'
              ? {
                  account_engine: accountEngine,
                  // simple mode always real-merges; config mode is view-only unless unchecked.
                  dry_run: accountEngine === 'config' ? configDryRun : false,
                  ...(accountEngine === 'config' ? { match_profile: matchProfile } : {}),
                }
              : {}),
          },
        }),
      })

      if (!response.ok) {
        const data = await response.json()
        throw new Error(data.detail || 'Failed to start scan')
      }

      const data = await response.json()
      router.push(`/scan/${data.scan_id}`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred')
    } finally {
      setIsLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <AppNav />
      <div className="max-w-3xl mx-auto py-12 px-4">
        {/* Header */}
        <div className="mb-8">
          <button
            onClick={() => router.push('/connect')}
            className="text-sm text-gray-500 hover:text-gray-700 mb-4"
          >
            &larr; Back to dashboard
          </button>
          <h1 className="text-2xl font-bold text-gray-900">Configure Deduplication Scan</h1>
          <p className="text-gray-600 mt-1">
            Connected to {connection.crm_type === 'hubspot' ? 'HubSpot' : 'Salesforce'}
            {connection.portal_id && ` (Portal ${connection.portal_id})`}
          </p>
        </div>

        <div className="space-y-6">
          {/* Object Type Selection */}
          <div className="bg-white rounded-lg shadow p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-4">
              1. Select Object Type
            </h2>
            <div className="grid gap-3">
              {objectTypes.map((type) => (
                <label
                  key={type.value}
                  className={`flex items-center p-4 border-2 rounded-lg cursor-pointer transition-colors ${
                    objectType === type.value
                      ? 'border-blue-500 bg-blue-50'
                      : type.available
                      ? 'border-gray-200 hover:border-gray-300'
                      : 'border-gray-100 bg-gray-50 cursor-not-allowed opacity-50'
                  }`}
                >
                  <input
                    type="radio"
                    name="objectType"
                    value={type.value}
                    checked={objectType === type.value}
                    onChange={(e) => setObjectType(e.target.value as ObjectType)}
                    disabled={!type.available}
                    className="sr-only"
                  />
                  <div>
                    <p className="font-medium text-gray-900">
                      {type.label}
                      {!type.available && <span className="ml-2 text-xs text-gray-500">(Coming soon)</span>}
                    </p>
                    <p className="text-sm text-gray-500">{type.description}</p>
                  </div>
                </label>
              ))}
            </div>
          </div>

          {/* Account matching engine */}
          {objectType === 'accounts' && (
            <div className="bg-white rounded-lg shadow p-6 space-y-4">
              <h2 className="text-lg font-semibold text-gray-900">Account Matching</h2>
              <div className="grid gap-3">
                <label className={`flex items-start p-3 border-2 rounded-lg cursor-pointer transition-colors ${accountEngine === 'simple' ? 'border-blue-500 bg-blue-50' : 'border-gray-200 hover:border-gray-300'}`}>
                  <input type="radio" name="accountEngine" value="simple" checked={accountEngine === 'simple'} onChange={() => setAccountEngine('simple')} className="mt-1 mr-3" />
                  <div>
                    <p className="font-medium text-gray-900">Simple <span className="text-xs text-gray-500">(recommended)</span></p>
                    <p className="text-sm text-gray-500">Match on website domain + account name. Works on any org (standard fields only). Real merge.</p>
                  </div>
                </label>
                <label className={`flex items-start p-3 border-2 rounded-lg cursor-pointer transition-colors ${accountEngine === 'config' ? 'border-blue-500 bg-blue-50' : 'border-gray-200 hover:border-gray-300'}`}>
                  <input type="radio" name="accountEngine" value="config" checked={accountEngine === 'config'} onChange={() => setAccountEngine('config')} className="mt-1 mr-3" />
                  <div>
                    <p className="font-medium text-gray-900">Config-driven <span className="text-xs text-gray-500">(client-tuned)</span></p>
                    <p className="text-sm text-gray-500">Fingerprints, discriminators, and hierarchy via a profile. Requires the org&apos;s custom fields (e.g. Scandit).</p>
                  </div>
                </label>
              </div>

              {accountEngine === 'config' && (
                <div className="border-t pt-4 space-y-3">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Match Profile</label>
                    <select
                      value={matchProfile}
                      onChange={(e) => setMatchProfile(e.target.value)}
                      className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500"
                    >
                      {ACCOUNT_PROFILES.map((p) => (
                        <option key={p.value} value={p.value}>{p.label}</option>
                      ))}
                    </select>
                  </div>
                  <label className="flex items-center gap-2 text-sm text-gray-700">
                    <input type="checkbox" checked={configDryRun} onChange={(e) => setConfigDryRun(e.target.checked)} className="h-4 w-4 rounded border-gray-300" />
                    View-only (dry-run) — no records are merged
                  </label>
                  {!configDryRun && (
                    <p className="text-sm text-amber-700">⚠ Real merge enabled — approved sets will be permanently merged in Salesforce.</p>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Lead -> Contact conversion explainer */}
          {objectType === 'lead_conversion' && (
            <div className="bg-white rounded-lg shadow p-6 space-y-3">
              <h2 className="text-lg font-semibold text-gray-900">Lead → Contact Conversion</h2>
              <p className="text-sm text-gray-600">
                We match each unconverted lead against your existing contacts (by email and name).
                For every match you approve, the lead is <strong>converted into that existing contact</strong> —
                passing the contact&apos;s account so it works even when the contact already exists
                (the usual API blocker). Only <strong>empty</strong> fields on the contact are filled from the lead.
              </p>
              <div className="bg-amber-50 border border-amber-200 rounded-md p-3">
                <p className="text-sm text-amber-800">
                  ⚠ Conversion is <strong>irreversible</strong> — a converted lead becomes read-only in Salesforce.
                  Nothing runs until you review and approve each match. A pre-action snapshot of every lead is
                  captured for your records. No new opportunities are created.
                </p>
              </div>
            </div>
          )}

          {/* Winner Rules — the survivor selection doesn't apply to conversion
              (the existing contact is always the survivor), so it's hidden there. */}
          {objectType !== 'lead_conversion' && (
          <div className="bg-white rounded-lg shadow p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-2">
              2. Configure Winner Rules
            </h2>
            <p className="text-sm text-gray-500 mb-4">
              When duplicates are found, these rules determine which record becomes the &quot;winner&quot;.
              Rules are applied in priority order.
            </p>

            <div className="space-y-4">
              {winnerRules.map((rule, index) => (
                <div key={index} className="flex items-start gap-4 p-4 bg-gray-50 rounded-lg">
                  <div className="flex-shrink-0 w-8 h-8 bg-blue-100 text-blue-600 rounded-full flex items-center justify-center font-medium">
                    {index + 1}
                  </div>
                  <div className="flex-1 space-y-3">
                    <select
                      value={rule.type}
                      onChange={(e) => updateRule(index, { type: e.target.value as WinnerRuleType })}
                      className="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500"
                    >
                      {WINNER_RULES.map((r) => (
                        <option key={r.value} value={r.value}>
                          {r.label}
                        </option>
                      ))}
                    </select>
                    <p className="text-xs text-gray-500">
                      {WINNER_RULES.find(r => r.value === rule.type)?.description}
                    </p>

                    {rule.type === 'custom_field' && (
                      <div className="grid grid-cols-2 gap-3">
                        <input
                          type="text"
                          placeholder="Field name (e.g., lifecyclestage)"
                          value={rule.customField || ''}
                          onChange={(e) => updateRule(index, { ...rule, customField: e.target.value })}
                          className="px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500"
                        />
                        <input
                          type="text"
                          placeholder="Value to match (e.g., customer)"
                          value={rule.customValue || ''}
                          onChange={(e) => updateRule(index, { ...rule, customValue: e.target.value })}
                          className="px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-blue-500 focus:border-blue-500"
                        />
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
          )}

          {/* Confidence Threshold */}
          <div className="bg-white rounded-lg shadow p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-2">
              {objectType === 'lead_conversion' ? '2. Match Confidence Threshold' : '3. Match Confidence Threshold'}
            </h2>
            <p className="text-sm text-gray-500 mb-4">
              Only records with similarity above this threshold will be flagged as duplicates.
            </p>

            <div className="space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-gray-500">Low (more matches, lower precision)</span>
                <span className="font-medium text-gray-900">{confidenceThreshold}%</span>
                <span className="text-gray-500">High (fewer matches, higher precision)</span>
              </div>
              <input
                type="range"
                min="50"
                max="99"
                value={confidenceThreshold}
                onChange={(e) => setConfidenceThreshold(Number(e.target.value))}
                className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer"
              />
              <div className="flex justify-between text-xs text-gray-400">
                <span>50%</span>
                <span>75%</span>
                <span>99%</span>
              </div>
            </div>
          </div>

          {/* Error Display */}
          {error && (
            <div className="bg-red-50 text-red-800 p-4 rounded-lg">
              {error}
            </div>
          )}

          {/* Start Scan Button */}
          <button
            onClick={handleStartScan}
            disabled={isLoading}
            className="w-full py-4 px-6 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isLoading ? 'Starting Scan...' : 'Start Deduplication Scan'}
          </button>

          {/* Info */}
          <div className="bg-yellow-50 rounded-lg p-4">
            <h3 className="font-medium text-yellow-900 mb-2">What happens next?</h3>
            <ul className="text-sm text-yellow-800 space-y-1 list-disc list-inside">
              <li>We&apos;ll fetch all {OBJECT_LABEL[objectType]} from your CRM</li>
              {objectType === 'lead_conversion' ? (
                <>
                  <li>Each lead is matched to your existing contacts using fuzzy matching on names and emails</li>
                  <li>You&apos;ll review each match before any lead is converted</li>
                  <li>This process is safe — nothing is converted until you approve</li>
                </>
              ) : (
                <>
                  <li>Duplicates will be detected using fuzzy matching on {objectType === 'companies' || objectType === 'accounts' ? 'name and web domain' : 'names and emails'}</li>
                  <li>You&apos;ll review each duplicate set before any changes are made</li>
                  <li>This process is safe - no data is modified until you approve</li>
                </>
              )}
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}
