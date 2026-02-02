'use client'

import Link from 'next/link'
import { Menu } from 'lucide-react'
import { useState } from 'react'

export default function Header({ scrolled }: { scrolled: boolean }) {
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)

  return (
    <header
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${
        scrolled
          ? 'bg-white shadow-md'
          : 'bg-white/80 backdrop-blur-sm'
      }`}
    >
      <nav className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-16 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-blue-600 rounded-lg flex items-center justify-center">
            <span className="text-white font-bold text-lg">O</span>
          </div>
          <span className="font-bold text-xl">OppTick</span>
        </div>

        <div className="hidden md:flex items-center gap-8">
          <a href="#features" className="text-gray-600 hover:text-gray-900 text-sm font-medium">Features</a>
          <a href="#how-it-works" className="text-gray-600 hover:text-gray-900 text-sm font-medium">How It Works</a>
          <Link href="https://t.me/opptickbot" target="_blank" className="bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 transition text-sm font-medium">
            Start with Telegram
          </Link>
        </div>

        <button
          onClick={() => setMobileMenuOpen(!mobileMenuOpen)}
          className="md:hidden p-2"
        >
          <Menu className="w-5 h-5" />
        </button>
      </nav>

      {mobileMenuOpen && (
        <div className="md:hidden border-t bg-white">
          <div className="px-4 py-4 space-y-2">
            <a href="#features" className="block text-gray-600 hover:text-gray-900 text-sm font-medium py-2">Features</a>
            <a href="#how-it-works" className="block text-gray-600 hover:text-gray-900 text-sm font-medium py-2">How It Works</a>
            <Link href="https://t.me/opptickbot" target="_blank" className="block bg-blue-600 text-white px-4 py-2 rounded-lg hover:bg-blue-700 transition text-sm font-medium text-center">
              Start with Telegram
            </Link>
          </div>
        </div>
      )}
    </header>
  )
}
