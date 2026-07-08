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

type ObjectType = 'contacts' | 'accounts' | 'companies' | 'deals'
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
    { value: 'accounts', label: 'Accounts', description: 'Organization records — config-driven match, view-only (dry-run)', available: true },
    { value: 'deals', label: 'Deals', description: 'Sales pipeline records', available: false },
  ]
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
            ...(objectType === 'accounts' ? { match_profile: matchProfile } : {}),
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

          {/* Match Profile (accounts dry-run) */}
          {objectType === 'accounts' && (
            <div className="bg-white rounded-lg shadow p-6">
              <h2 className="text-lg font-semibold text-gray-900 mb-2">Match Profile</h2>
              <p className="text-sm text-gray-500 mb-4">
                Accounts use a config-driven, client-specific matching profile (name + custom-field
                discriminators + deterministic keys). This run is <strong>view-only</strong> — no records are merged.
              </p>
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
          )}

          {/* Winner Rules */}
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

          {/* Confidence Threshold */}
          <div className="bg-white rounded-lg shadow p-6">
            <h2 className="text-lg font-semibold text-gray-900 mb-2">
              3. Match Confidence Threshold
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
              <li>We&apos;ll fetch all {objectType} from your CRM</li>
              <li>Duplicates will be detected using fuzzy matching on {objectType === 'companies' ? 'company name and web domain' : 'names and emails'}</li>
              <li>You&apos;ll review each duplicate set before any changes are made</li>
              <li>This process is safe - no data is modified until you approve</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}
