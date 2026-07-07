import { redirect } from 'next/navigation'
import { createClient } from '@/lib/supabase/server'
import ScanConfigClient from './ScanConfigClient'

export default async function ScanPage({
  searchParams,
}: {
  searchParams: Promise<{ connection?: string }>
}) {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()

  if (!user) {
    redirect('/login')
  }

  const { connection: connectionId } = await searchParams

  // Multi-org: scan a SPECIFIC connected org when given; otherwise fall back to
  // the user's only org, or send them to the dashboard to pick one.
  const { data: connections } = await supabase
    .from('crm_connections')
    .select('*')
    .eq('user_id', user.id)
    .order('created_at', { ascending: true })

  const all = connections ?? []
  const connection = connectionId ? all.find((c) => c.id === connectionId) : all[0]

  if (!connection) {
    redirect('/connect')
  }

  return <ScanConfigClient userId={user.id} connection={connection} />
}
