import { NewsItem } from '@/app/lib/use_signal_stream';

function sentimentColor(s: number): string {
  if (s > 0.15) return '#35D0A0';
  if (s < -0.15) return '#E8A23D';
  return '#7C8B9C';
}

export default function NewsFeed({ news }: { news: NewsItem[] }) {
  return (
    <div className="border border-[#233042] bg-[#161F2B]">
      <div className="border-b border-[#233042] px-4 py-3 text-sm text-[#7C8B9C]">
        news feed - sentiment (display only, not yet in scoring)
      </div>
      <div className="max-h-80 overflow-y-auto">
        {news.length === 0 && (
          <p className="px-4 py-6 text-sm text-[#7C8B9C]">Waiting for headlines...</p>
        )}
        {news.map((n, i) => (
          <a
            key={`${n.timestamp}-${n.headline}-${i}`}
            href={n.link || '#'}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-start gap-3 border-b border-[#1B2531] px-4 py-2.5 text-sm last:border-b-0 hover:bg-[#1B2531]"
          >
            <span
              className="mt-1 h-2 w-2 flex-shrink-0 rounded-full"
              style={{ backgroundColor: sentimentColor(n.sentiment) }}
            />
            <div>
              <p className="text-[#E7ECF2]">{n.headline}</p>
              <p className="mt-0.5 text-xs text-[#7C8B9C]">
                {n.source || 'news'} - {n.symbols.join(', ') || 'unlinked'} - {n.sentiment.toFixed(2)}
              </p>
            </div>
          </a>
        ))}
      </div>
    </div>
  );
}
