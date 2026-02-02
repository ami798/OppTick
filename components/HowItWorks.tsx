'use client'

export default function HowItWorks() {
  const steps = [
    {
      number: '1',
      title: 'Forward a Message',
      description: 'Forward an opportunity message from any Telegram channel to OppTick bot.',
    },
    {
      number: '2',
      title: 'Confirm Details',
      description: 'OppTick auto-detects the deadline. Verify or adjust, then set type and priority.',
    },
    {
      number: '3',
      title: 'Get Reminders',
      description: 'Receive smart reminders as the deadline approaches so you never miss it.',
    },
    {
      number: '4',
      title: 'Stay Organized',
      description: 'View all opportunities in one place, archive completed ones, and get weekly summaries.',
    },
  ]

  return (
    <section id="how-it-works" className="py-20 px-4 sm:px-6 lg:px-8 max-w-7xl mx-auto">
      <div className="text-center mb-16">
        <h2 className="text-4xl font-bold text-gray-900 mb-4">How It Works</h2>
        <p className="text-lg text-gray-600">Simple, intuitive, and fast</p>
      </div>

      <div className="grid md:grid-cols-4 gap-6">
        {steps.map((step, i) => (
          <div key={i} className="relative">
            <div className="bg-white border-2 border-blue-600 rounded-xl p-6 h-full">
              <div className="bg-blue-600 text-white w-10 h-10 rounded-full flex items-center justify-center font-bold text-lg mb-4">
                {step.number}
              </div>
              <h3 className="text-lg font-semibold text-gray-900 mb-2">{step.title}</h3>
              <p className="text-gray-600">{step.description}</p>
            </div>
            {i < steps.length - 1 && (
              <div className="hidden md:block absolute top-1/2 -right-3 transform -translate-y-1/2 text-blue-600 text-2xl">→</div>
            )}
          </div>
        ))}
      </div>

      <div className="mt-16 bg-blue-50 border-2 border-blue-200 rounded-xl p-8 sm:p-12 text-center">
        <h3 className="text-2xl font-bold text-gray-900 mb-4">Start Tracking Opportunities Today</h3>
        <p className="text-gray-600 mb-6 max-w-2xl mx-auto">
          Join hundreds of students and professionals who never miss opportunities. Add the OppTick Telegram bot to your account now.
        </p>
        <a
          href="https://t.me/opptickbot"
          target="_blank"
          rel="noopener noreferrer"
          className="inline-block bg-blue-600 text-white px-8 py-3 rounded-lg hover:bg-blue-700 transition font-semibold"
        >
          Add OppTick Bot
        </a>
      </div>
    </section>
  )
}
