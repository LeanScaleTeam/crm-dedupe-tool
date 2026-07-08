'use client'

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
  created_at?: string
  association_count?: number
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
}

interface DuplicateCardProps {
  duplicateSet: DuplicateSet
  isSelected: boolean
  onToggleSelect: () => void
  onToggleExclude: (excluded: boolean) => void
  onExpand: () => void
}

// Records may be contacts OR companies — fall back to the company name.
function getContactName(record: Contact): string {
  if (record.full_name) return record.full_name
  const person = [record.first_name, record.last_name].filter(Boolean).join(' ')
  return person || record.name || record.company || 'Unknown'
}

// Subtitle: email for contacts, web domain/website for companies.
function getSubtitle(record: Contact): string {
  return record.email || record.domain || record.website || '—'
}

function getConfidenceColor(confidence: number): string {
  if (confidence >= 90) return 'bg-green-100 text-green-800'
  if (confidence >= 70) return 'bg-yellow-100 text-yellow-800'
  return 'bg-red-100 text-red-800'
}

export default function DuplicateCard({
  duplicateSet,
  isSelected,
  onToggleSelect,
  onToggleExclude,
  onExpand,
}: DuplicateCardProps) {
  const winner = duplicateSet.winner_data
  const losers = duplicateSet.loser_data
  const allContacts = [winner, ...losers]
  const totalRecords = allContacts.length

  return (
    <div
      className={`bg-white rounded-lg shadow p-4 border-2 transition-colors ${
        duplicateSet.excluded
          ? 'border-gray-200 opacity-60'
          : isSelected
          ? 'border-blue-500'
          : 'border-transparent hover:border-gray-200'
      }`}
    >
      <div className="flex items-start gap-4">
        {/* Checkbox */}
        {!duplicateSet.excluded && (
          <input
            type="checkbox"
            checked={isSelected}
            onChange={onToggleSelect}
            className="mt-1 h-5 w-5 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
          />
        )}

        {/* Main Content */}
        <div className="flex-1 min-w-0">
          {/* Header Row */}
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center gap-3">
              <span className={`px-2 py-1 rounded-full text-xs font-medium ${getConfidenceColor(duplicateSet.confidence)}`}>
                {duplicateSet.confidence.toFixed(0)}% match
              </span>
              <span className="text-sm text-gray-500">
                {totalRecords} records
              </span>
              {duplicateSet.merged && (
                <span className="px-2 py-1 bg-blue-100 text-blue-800 rounded-full text-xs font-medium">
                  Merged
                </span>
              )}
              {duplicateSet.excluded && (
                <span className="px-2 py-1 bg-gray-100 text-gray-600 rounded-full text-xs font-medium">
                  Excluded
                </span>
              )}
            </div>

            <div className="flex items-center gap-2">
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  onToggleExclude(!duplicateSet.excluded)
                }}
                className="text-sm text-gray-500 hover:text-gray-700"
              >
                {duplicateSet.excluded ? 'Include' : 'Exclude'}
              </button>
              <button
                onClick={onExpand}
                className="text-sm text-blue-600 hover:text-blue-700 font-medium"
              >
                Details
              </button>
            </div>
          </div>

          {/* Contact Preview */}
          <div className="flex items-center gap-6">
            {/* Winner */}
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 bg-green-100 rounded-full flex items-center justify-center">
                <span className="text-green-600 text-xs font-bold">W</span>
              </div>
              <div>
                <p className="font-medium text-gray-900 truncate">
                  {getContactName(winner)}
                </p>
                <p className="text-sm text-gray-500 truncate">
                  {getSubtitle(winner)}
                </p>
              </div>
            </div>

            {/* Arrow */}
            <span className="text-gray-400">↔</span>

            {/* Losers */}
            {losers.slice(0, 2).map((loser, idx) => (
              <div key={idx} className="flex items-center gap-2">
                <div className="w-8 h-8 bg-gray-100 rounded-full flex items-center justify-center">
                  <span className="text-gray-500 text-xs font-bold">L</span>
                </div>
                <div>
                  <p className="font-medium text-gray-700 truncate">
                    {getContactName(loser)}
                  </p>
                  <p className="text-sm text-gray-500 truncate">
                    {getSubtitle(loser)}
                  </p>
                </div>
              </div>
            ))}

            {losers.length > 2 && (
              <span className="text-sm text-gray-500">
                +{losers.length - 2} more
              </span>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
