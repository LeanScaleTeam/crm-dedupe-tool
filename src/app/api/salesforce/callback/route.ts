import { NextResponse } from 'next/server'
import { createClient } from '@/lib/supabase/server'

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url)
  const code = searchParams.get('code')

  // Use configured site URL to avoid Netlify deploy-preview URL mismatch
  const siteUrl = process.env.NEXT_PUBLIC_SITE_URL || process.env.URL || new URL(request.url).origin

  if (!code) {
    return NextResponse.redirect(`${siteUrl}/connect?error=no_code`)
  }

  // Get current user + session (the backend now authenticates via the access token)
  const supabase = await createClient()
  const { data: { session } } = await supabase.auth.getSession()

  if (!session?.user) {
    return NextResponse.redirect(`${siteUrl}/login`)
  }

  try {
    // Exchange code for tokens via our Python backend
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'
    const response = await fetch(`${apiUrl}/salesforce/exchange-token`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${session.access_token}`,
      },
      body: JSON.stringify({
        code,
        redirect_uri: `${siteUrl}/api/salesforce/callback`,
      }),
    })

    if (!response.ok) {
      const error = await response.json()
      console.error('Token exchange failed:', error)
      return NextResponse.redirect(`${siteUrl}/connect?error=token_exchange_failed`)
    }

    // Success - redirect to connect page
    return NextResponse.redirect(`${siteUrl}/connect?connected=true`)
  } catch (error) {
    console.error('Salesforce callback error:', error)
    return NextResponse.redirect(`${siteUrl}/connect?error=connection_failed`)
  }
}
