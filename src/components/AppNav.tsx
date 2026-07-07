'use client'

import { useRouter, usePathname } from 'next/navigation'
import { createClient } from '@/lib/supabase/client'

/** Persistent top nav so Orgs and Reports are always reachable. */
export default function AppNav() {
  const router = useRouter()
  const pathname = usePathname()

  const handleLogout = async () => {
    const supabase = createClient()
    await supabase.auth.signOut()
    router.push('/login')
  }

  const NavLink = ({ href, label }: { href: string; label: string }) => {
    const active = href === '/connect' ? pathname === '/connect' : pathname?.startsWith(href)
    return (
      <button
        onClick={() => router.push(href)}
        className={`text-sm px-3 py-1.5 rounded-md transition-colors ${
          active ? 'bg-blue-50 text-blue-700 font-medium' : 'text-gray-600 hover:text-gray-900'
        }`}
      >
        {label}
      </button>
    )
  }

  return (
    <header className="bg-white border-b border-gray-200 sticky top-0 z-40">
      <div className="max-w-4xl mx-auto px-4 h-14 flex items-center justify-between">
        <button
          onClick={() => router.push('/connect')}
          className="font-bold text-gray-900 hover:text-gray-700"
        >
          CRM Dedup Tool
        </button>
        <nav className="flex items-center gap-1">
          <NavLink href="/connect" label="Orgs" />
          <NavLink href="/reports" label="Reports" />
          <button
            onClick={handleLogout}
            className="text-sm px-3 py-1.5 text-gray-500 hover:text-gray-700"
          >
            Sign out
          </button>
        </nav>
      </div>
    </header>
  )
}
