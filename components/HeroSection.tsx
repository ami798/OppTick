'use client'

import Link from 'next/link'
import { ArrowRight } from 'lucide-react'

export default function HeroSection() {
  return (
    <section className="pt-32 pb-20 px-4 sm:px-6 lg:px-8 max-w-7xl mx-auto">
      <div className="text-center max-w-3xl mx-auto">
        <h1 className="text-5xl sm:text-6xl font-bold text-gray-900 mb-6">
          Never Miss an Opportunity
        </h1>
        <p className="text-xl text-gray-600 mb-8">
          Forward opportunities from Telegram channels. OppTick automatically detects deadlines, prioritizes them, and sends you intelligent reminders so you never miss out.
        </p>
        <div className="flex flex-col sm:flex-row gap-4 justify-center">
          <Link
            href="https://t.me/opptickbot"
            target="_blank"
            className="bg-blue-600 text-white px-8 py-3 rounded-lg hover:bg-blue-700 transition font-semibold flex items-center justify-center gap-2"
          >
            Start Now <ArrowRight className="w-4 h-4" />
          </Link>
          <Link
            href="#how-it-works"
            className="border-2 border-gray-300 text-gray-900 px-8 py-3 rounded-lg hover:border-gray-900 transition font-semibold"
          >
            Learn More
          </Link>
        </div>
      </div>

      <div className="mt-16 bg-gradient-to-br from-blue-50 to-indigo-100 rounded-xl p-8 sm:p-12 border border-blue-200">
        <div className="grid md:grid-cols-3 gap-8">
          <div className="text-center">
            <div className="text-4xl font-bold text-blue-600 mb-2">3+</div>
            <p className="text-gray-600">Opportunity Types</p>
          </div>
          <div className="text-center">
            <div className="text-4xl font-bold text-blue-600 mb-2">AI-Powered</div>
            <p className="text-gray-600">Deadline Detection</p>
          </div>
          <div className="text-center">
            <div className="text-4xl font-bold text-blue-600 mb-2">Smart</div>
            <p className="text-gray-600">Priority Reminders</p>
          </div>
        </div>
      </div>
    </section>
  )
}
