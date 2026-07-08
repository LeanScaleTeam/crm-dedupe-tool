'use client'

import { useState, useCallback, useEffect } from 'react'
import { apiFetch } from '@/lib/api'

// Represents one CRM record in a duplicate set. Covers BOTH contacts and
// companies — company records populate the organization fields instead.
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
  // Company fields:
  name?: string
  domain?: string
  website?: string
  industry?: string
  country?: string
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

// An associated (related) record fetched from the CRM for context.
interface RelatedRecord {
  id?: string
  properties?: Record<string, string>
}

interface DuplicateDetailProps {
  duplicateSet: DuplicateSet
  scanId: string
  onClose: () => void
  onPreviewUpdated?: (setId: string, preview: Record<string, unknown>) => void
}

// Records may be contacts OR companies — fall back to the company name.
function getContactName(record: Contact): string {
  if (record.full_name) return record.full_name
  const person = [record.first_name, record.last_name].filter(Boolean).join(' ')
  return person || record.name || record.company || 'Unknown'
}

function formatDate(dateStr?: string): string {
  if (!dateStr) return '-'
  try {
    return new Date(dateStr).toLocaleDateString()
  } catch {
    return '-'
  }
}

// Editable fields the user can pick values for — contacts vs companies.
const CONTACT_EDITABLE_FIELDS: { key: string; label: string }[] = [
  { key: 'email', label: 'Email' },
  { key: 'first_name', label: 'First Name' },
  { key: 'last_name', label: 'Last Name' },
  { key: 'phone', label: 'Phone' },
  { key: 'company', label: 'Company' },
  { key: 'job_title', label: 'Job Title' },
]

const COMPANY_EDITABLE_FIELDS: { key: string; label: string }[] = [
  { key: 'name', label: 'Company Name' },
  { key: 'domain', label: 'Domain' },
  { key: 'website', label: 'Website' },
  { key: 'phone', label: 'Phone' },
  { key: 'industry', label: 'Industry' },
  { key: 'country', label: 'Country' },
]

// Company records carry organization fields (domain/industry/website) that
// contacts never have — detect the shape so the modal shows the right rows.
function isCompanyRecord(record: Contact): boolean {
  return 'domain' in record || 'industry' in record || 'website' in record
}

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
  // Winner is changeable in the UI: derive it from a local winnerId (defaults to the
  // scan-selected winner). allRecords = the original winner + all losers.
  const allRecords = [duplicateSet.winner_data, ...duplicateSet.loser_data]
  const [winnerId, setWinnerId] = useState<string | undefined>(
    duplicateSet.winner_record_id ?? duplicateSet.winner_data?.id
  )
  const winner = allRecords.find(r => r.id === winnerId) ?? duplicateSet.winner_data
  const losers = allRecords.filter(r => r.id !== winner.id)
  const allContacts = [winner, ...losers]
  // Contacts and companies expose different editable fields; pick by record shape.
  const EDITABLE_FIELDS = isCompanyRecord(winner) ? COMPANY_EDITABLE_FIELDS : CONTACT_EDITABLE_FIELDS
  // Every other populated property (from raw_properties), shown read-only for context.
  const editableKeySet = new Set(EDITABLE_FIELDS.map(f => f.key))
  const allPropKeys = collectPropKeys(allContacts, editableKeySet)

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
  const [saveError, setSaveError] = useState<string | null>(null)

  // Related records (associated contacts/deals/companies) fetched when the modal opens.
  const [related, setRelated] = useState<Record<string, Record<string, RelatedRecord[]>>>({})
  const [relatedLoading, setRelatedLoading] = useState(true)
  const [showAllProps, setShowAllProps] = useState(true)
  const [writableFields, setWritableFields] = useState<Set<string>>(new Set())

  useEffect(() => {
    let cancelled = false
    const ids = allContacts.map(c => c.id).filter(Boolean) as string[]
    Promise.all(
      ids.map(async (id) => {
        try {
          const resp = await apiFetch(`/scan/${scanId}/records/${id}/associations`)
          if (!resp.ok) return [id, {} as Record<string, RelatedRecord[]>] as const
          const data = await resp.json()
          return [id, (data.related || {}) as Record<string, RelatedRecord[]>] as const
        } catch {
          return [id, {} as Record<string, RelatedRecord[]>] as const
        }
      })
    ).then((entries) => {
      if (!cancelled) {
        setRelated(Object.fromEntries(entries))
        setRelatedLoading(false)
      }
    })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scanId, duplicateSet.id])

  // Which raw properties are writable — those become pick-to-merge fields.
  useEffect(() => {
    let cancelled = false
    apiFetch(`/scan/${scanId}/editable-fields`)
      .then(r => (r.ok ? r.json() : { fields: [] }))
      .then((data: { fields?: { name: string }[] }) => {
        if (!cancelled) setWritableFields(new Set((data.fields || []).map(f => f.name)))
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [scanId])

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

  // Promote a record to winner (the survivor). The old winner becomes a loser; a
  // promoted record can't also be marked "not a duplicate".
  const makeWinner = useCallback((recordId?: string) => {
    if (!recordId) return
    setWinnerId(recordId)
    setExcludedIds(prev => {
      if (!prev.has(recordId)) return prev
      const next = new Set(prev)
      next.delete(recordId)
      return next
    })
    setHasChanges(true)
  }, [])

  const excludedCount = losers.filter(l => l.id && excludedIds.has(l.id)).length
  const mergingCount = allContacts.length - excludedCount // winner + kept losers

  const saveChanges = useCallback(async () => {
    setIsSaving(true)
    setSaveError(null)
    try {
      const response = await apiFetch(
        `/scan/${scanId}/duplicate-sets/${duplicateSet.id}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            merged_preview: mergedPreview,
            excluded_record_ids: Array.from(excludedIds),
            winner_record_id: winnerId,
          }),
        }
      )
      if (response.ok) {
        setHasChanges(false)
        onPreviewUpdated?.(duplicateSet.id, mergedPreview)
      } else {
        const data = await response.json().catch(() => ({}))
        setSaveError(data.detail || `Save failed (${response.status}).`)
      }
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : 'Save failed.')
    } finally {
      setIsSaving(false)
    }
  }, [scanId, duplicateSet.id, mergedPreview, excludedIds, winnerId, onPreviewUpdated])

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      {/* Backdrop */}
      <div className="fixed inset-0 bg-black bg-opacity-50" onClick={onClose} />

      {/* Modal */}
      <div className="relative min-h-screen flex items-center justify-center p-4">
        <div className="relative bg-white rounded-xl shadow-xl max-w-5xl w-full max-h-[90vh] flex flex-col overflow-hidden">
          {/* Header */}
          <div className="flex-shrink-0 bg-white border-b px-6 py-4 flex items-center justify-between z-10">
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
          <div className="p-6 overflow-y-auto flex-1 min-h-0">
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
                          <div className="mt-1 flex items-center gap-3">
                            <button
                              onClick={() => makeWinner(loser.id)}
                              className="text-xs font-medium text-green-600 hover:text-green-700"
                            >
                              ★ Make winner
                            </button>
                            <button
                              onClick={() => toggleExcluded(loser.id)}
                              className={`text-xs font-medium ${isExcluded ? 'text-blue-600 hover:text-blue-700' : 'text-red-500 hover:text-red-600'}`}
                            >
                              {isExcluded ? '↺ Include' : '✕ Not a duplicate'}
                            </button>
                          </div>
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

                  {/* All other populated properties (read-only context) */}
                  {allPropKeys.length > 0 && (
                    <tr>
                      <td colSpan={allContacts.length + 2} className="py-2">
                        <button
                          onClick={() => setShowAllProps(v => !v)}
                          className="text-xs text-gray-500 hover:text-gray-700 uppercase tracking-wider px-4"
                        >
                          {showAllProps ? '▾' : '▸'} All properties ({allPropKeys.length}) — ✎ = editable (click a value to pick it)
                        </button>
                      </td>
                    </tr>
                  )}
                  {showAllProps && allPropKeys.map((key) => {
                    const writable = writableFields.has(key)
                    const mergedRaw = mergedPreview[key] as string | undefined
                    const mergedDisplay = writable ? (mergedRaw ?? rawVal(winner, key)) : rawVal(winner, key)
                    return (
                      <tr key={`prop-${key}`} className="border-b">
                        <td className="py-2 px-4 text-xs font-medium text-gray-500 break-all">
                          {key}
                          {writable && (
                            <span className="ml-1 text-blue-400" title="Editable — click a value to use it">✎</span>
                          )}
                        </td>
                        {allContacts.map((contact, idx) => {
                          const val = rawVal(contact, key)
                          const colExcluded = idx > 0 && !!contact.id && excludedIds.has(contact.id)
                          const isSelected = writable && !!val && val === mergedDisplay
                          const pickable = writable && !!val && !colExcluded
                          return (
                            <td
                              key={idx}
                              onClick={() => { if (pickable) pickFieldValue(key, val) }}
                              title={pickable ? 'Click to use this value' : ''}
                              className={`py-2 px-4 text-xs break-all ${idx === 0 ? 'bg-green-50' : 'bg-gray-50'} ${colExcluded ? 'opacity-40 line-through' : ''} ${
                                isSelected && !colExcluded
                                  ? 'ring-2 ring-inset ring-blue-500 font-medium text-blue-900'
                                  : val ? 'text-gray-700' : 'text-gray-300'
                              } ${pickable ? 'cursor-pointer hover:bg-blue-50' : ''}`}
                            >
                              {val || '-'}
                              {isSelected && !colExcluded && <span className="ml-1 text-blue-500">&#10003;</span>}
                            </td>
                          )
                        })}
                        <td className={`py-2 px-4 text-xs bg-blue-50 break-all ${writable ? 'text-gray-900 font-medium' : 'text-gray-700'}`}>
                          {mergedDisplay || '-'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            {/* Related records */}
            <div className="mt-6">
              <h3 className="text-sm font-medium text-gray-700 mb-3">Related records</h3>
              {relatedLoading ? (
                <p className="text-sm text-gray-400">Loading related records…</p>
              ) : (
                <div
                  className="grid gap-4"
                  style={{ gridTemplateColumns: `repeat(${allContacts.length}, minmax(0, 1fr))` }}
                >
                  {allContacts.map((rec, idx) => {
                    const rel: Record<string, RelatedRecord[]> = (rec.id ? related[rec.id] : undefined) || {}
                    const isExcluded = idx > 0 && !!rec.id && excludedIds.has(rec.id)
                    const groups = Object.entries(rel).filter(([, items]) => items && items.length > 0)
                    return (
                      <div
                        key={idx}
                        className={`border rounded-lg p-3 ${idx === 0 ? 'border-green-200 bg-green-50/40' : 'border-gray-200'} ${isExcluded ? 'opacity-40' : ''}`}
                      >
                        <p className="text-xs font-semibold text-gray-700 mb-2 truncate">
                          {idx === 0 ? '★ ' : ''}{getContactName(rec)}
                        </p>
                        {groups.length === 0 ? (
                          <p className="text-xs text-gray-400">No related records</p>
                        ) : (
                          groups.map(([toObject, items]) => (
                            <div key={toObject} className="mb-2">
                              <p className="text-[11px] uppercase tracking-wide text-gray-400 mb-1">
                                {toObject} ({items.length})
                              </p>
                              <ul className="space-y-1">
                                {items.slice(0, 8).map((it, i) => {
                                  const f = formatRelated(toObject, it.properties || {})
                                  return (
                                    <li key={i} className="text-xs text-gray-700 truncate">
                                      <span className="font-medium">{f.title}</span>
                                      {f.sub && <span className="text-gray-400"> · {f.sub}</span>}
                                    </li>
                                  )
                                })}
                                {items.length > 8 && (
                                  <li className="text-xs text-gray-400">+{items.length - 8} more</li>
                                )}
                              </ul>
                            </div>
                          ))
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
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
          <div className="flex-shrink-0 bg-white border-t px-6 py-4 flex justify-between items-center">
            <div className="text-sm text-gray-500">
              {saveError ? (
                <span className="text-red-600">{saveError}</span>
              ) : hasChanges ? 'You have unsaved changes' : ''}
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

/** Union of every raw_properties key that is non-null on at least one record,
 *  excluding keys already shown as editable rows. Sorted for stable display. */
function collectPropKeys(records: Contact[], exclude: Set<string>): string[] {
  const keys = new Set<string>()
  for (const r of records) {
    const rp = r.raw_properties || {}
    for (const k of Object.keys(rp)) {
      if (exclude.has(k)) continue
      const v = rp[k]
      // Skip nested relationship/subquery values (e.g. Salesforce Account{},
      // Opportunities{}) — they aren't flat, single-value fields.
      if (v !== null && v !== undefined && v !== '' && typeof v !== 'object') keys.add(k)
    }
  }
  return Array.from(keys).sort()
}

/** A raw_properties value as a display string. */
function rawVal(contact: Contact, key: string): string {
  const v = contact.raw_properties?.[key]
  if (v === null || v === undefined || typeof v === 'object') return ''
  return String(v)
}

/** Format one related record into a title + subtitle for the given object type. */
function formatRelated(toObject: string, props: Record<string, string>): { title: string; sub: string } {
  if (toObject === 'contacts') {
    const name = [props.firstname, props.lastname].filter(Boolean).join(' ') || props.email || '(contact)'
    const sub = [props.jobtitle, name !== props.email ? props.email : ''].filter(Boolean).join(' · ')
    return { title: name, sub }
  }
  if (toObject === 'deals') {
    const amt = props.amount ? `$${Number(props.amount).toLocaleString()}` : ''
    return { title: props.dealname || '(deal)', sub: [amt, props.dealstage].filter(Boolean).join(' · ') }
  }
  if (toObject === 'companies') {
    return { title: props.name || props.domain || '(company)', sub: props.name && props.domain ? props.domain : '' }
  }
  return { title: '(record)', sub: '' }
}
