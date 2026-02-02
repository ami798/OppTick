'use client'

import { Bell, Zap, BarChart3 } from 'lucide-react'

export default function FeaturesSection() {
  const features = [
    {
      icon: Zap,
      title: 'Auto-Detect Deadlines',
      description: 'OppTick analyzes forwarded messages to automatically extract deadlines, eliminating manual entry.',
    },
    {
      icon: Bell,
      title: 'Smart Reminders',
      description: 'Receive reminders at 14, 7, 3, and 1 days before deadline. High priority opportunities get extra alerts.',
    },
    {
      icon: BarChart3,
      title: 'Track Everything',
      description: 'Organize opportunities by type (internship, scholarship, event) and priority level for easy management.',
    },
  ]

  return (
    <section id="features" className="py-20 px-4 sm:px-6 lg:px-8 max-w-7xl mx-auto">
      <div className="text-center mb-16">
        <h2 className="text-4xl font-bold text-gray-900 mb-4">Powerful Features</h2>
        <p className="text-lg text-gray-600">Everything you need to stay on top of opportunities</p>
      </div>

      <div className="grid md:grid-cols-3 gap-8">
        {features.map((feature, i) => {
          const Icon = feature.icon
          return (
            <div key={i} className="bg-white border border-gray-200 rounded-xl p-8 hover:shadow-lg transition">
              <div className="bg-blue-100 w-12 h-12 rounded-lg flex items-center justify-center mb-4">
                <Icon className="w-6 h-6 text-blue-600" />
              </div>
              <h3 className="text-xl font-semibold text-gray-900 mb-2">{feature.title}</h3>
              <p className="text-gray-600">{feature.description}</p>
            </div>
          )
        })}
      </div>

      <div className="mt-16 bg-gray-50 rounded-xl p-8 sm:p-12 border border-gray-200">
        <h3 className="text-2xl font-bold text-gray-900 mb-6">Supported Opportunity Types</h3>
        <div className="grid sm:grid-cols-2 md:grid-cols-4 gap-4">
          {['Internship', 'Scholarship', 'Event', 'Custom'].map((type) => (
            <div key={type} className="bg-white border border-gray-200 rounded-lg p-4 text-center font-medium text-gray-700">
              {type}
            </div>
          ))}
        </div>
      </div>
    </section>
  )
}
