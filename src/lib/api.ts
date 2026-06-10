// Authenticated fetch helper for the FastAPI backend.
//
// Phase 0 (safety): the backend now requires a verified Supabase access token on
// every protected endpoint and no longer trusts a `user_id` in the request body.
// Every browser → backend call must go through `apiFetch`, which attaches the
// current session's bearer token. Components pass only the path (e.g. '/scan/start').
import { createClient } from '@/lib/supabase/client'

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'

async function bearer(): Promise<string> {
  const supabase = createClient()
  const { data: { session } } = await supabase.auth.getSession()
  if (!session?.access_token) {
    throw new Error('Not authenticated — please sign in again.')
  }
  return session.access_token
}

/**
 * fetch() against the backend with the Supabase access token attached.
 * `path` is the backend path (a leading slash), not a full URL.
 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const token = await bearer()
  const headers = new Headers(init.headers || {})
  headers.set('Authorization', `Bearer ${token}`)
  return fetch(`${API_URL}${path}`, { ...init, headers })
}

/** Convenience: apiFetch + JSON parse, throwing on non-2xx with the API detail. */
export async function apiJson<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await apiFetch(path, init)
  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const body = await res.json()
      detail = body.detail || detail
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}
