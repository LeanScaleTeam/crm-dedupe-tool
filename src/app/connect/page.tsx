import { redirect } from 'next/navigation'
import { createClient } from '@/lib/supabase/server'
import ConnectClient from './ConnectClient'

export default async function ConnectPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; connected?: string }>
}) {
  const supabase = await createClient()
  const { data: { user } } = await supabase.auth.getUser()

  if (!user) {
    redirect('/login')
  }

  // All the user's connected orgs (multi-org).
  const { data: connections } = await supabase
    .from('crm_connections')
    .select('*')
    .eq('user_id', user.id)
    .order('created_at', { ascending: true })

  const { error, connected } = await searchParams

  return (
    <ConnectClient
      user={user}
      connections={connections ?? []}
      oauthError={error}
      oauthSuccess={!!connected}
    />
  )
}
