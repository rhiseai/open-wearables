import { useMemo } from 'react';
import { format, subDays, startOfDay } from 'date-fns';
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts';
import { ShieldCheck, Waves, CalendarDays, Heart } from 'lucide-react';
import { useSleepSummaries } from '@/hooks/api/use-health';
import { MetricCard } from '@/components/common/metric-card';
import { SectionHeader } from '@/components/common/section-header';
import {
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
} from '@/components/ui/chart';

// ---------------------------------------------------------------------------
// HRV-CV resilience score — mirrors backend app/utils/resilience_score.py
// Grosicki et al. (2026) doi:10.1152/ajpheart.00738.2025
// ---------------------------------------------------------------------------

const RESILIENCE_CONFIG = {
  minNights: 5,
  cvCeilingPct: 7.0,   // CV <= this -> score 100
  cvFloorPct: 40.0,    // CV >= this -> score 0
  eliteThresholdPct: 10.0,
  volatileThresholdPct: 25.0,
} as const;

interface ResilienceResult {
  score: number;
  cvPct: number;
  category: 'Elite Stability' | 'Normal' | 'Volatile';
  nightsUsed: number;
  avgHrv: number;
  nightlyHrv: { date: string; hrv: number }[];
}

function computeResilience(
  readings: { date: string; hrv: number }[]
): ResilienceResult | null {
  if (readings.length < RESILIENCE_CONFIG.minNights) return null;

  const values = readings.map((r) => r.hrv);
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  const variance =
    values.reduce((sum, v) => sum + (v - mean) ** 2, 0) / (values.length - 1);
  const stdev = Math.sqrt(variance);
  const cvPct = (stdev / mean) * 100;

  let score: number;
  if (cvPct <= RESILIENCE_CONFIG.cvCeilingPct) {
    score = 100;
  } else if (cvPct >= RESILIENCE_CONFIG.cvFloorPct) {
    score = 0;
  } else {
    const penaltyRatio =
      (cvPct - RESILIENCE_CONFIG.cvCeilingPct) /
      (RESILIENCE_CONFIG.cvFloorPct - RESILIENCE_CONFIG.cvCeilingPct);
    score = 100 - penaltyRatio * 100;
  }

  const category: ResilienceResult['category'] =
    cvPct <= RESILIENCE_CONFIG.eliteThresholdPct
      ? 'Elite Stability'
      : cvPct <= RESILIENCE_CONFIG.volatileThresholdPct
        ? 'Normal'
        : 'Volatile';

  return {
    score: Math.floor(score),
    cvPct: Math.round(cvPct * 10) / 10,
    category,
    nightsUsed: readings.length,
    avgHrv: Math.round(mean * 10) / 10,
    nightlyHrv: readings,
  };
}

// ---------------------------------------------------------------------------
// Visual helpers
// ---------------------------------------------------------------------------

const CATEGORY_STYLES = {
  'Elite Stability': {
    badge: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
    glow: 'shadow-[0_0_40px_rgba(16,185,129,0.2)]',
    ring: 'stroke-emerald-400',
    bar: '#10b981',
  },
  Normal: {
    badge: 'bg-sky-500/15 text-sky-400 border-sky-500/30',
    glow: 'shadow-[0_0_40px_rgba(14,165,233,0.2)]',
    ring: 'stroke-sky-400',
    bar: '#0ea5e9',
  },
  Volatile: {
    badge: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
    glow: 'shadow-[0_0_40px_rgba(244,63,94,0.2)]',
    ring: 'stroke-rose-400',
    bar: '#f43f5e',
  },
} as const;

function ScoreDial({ score, category }: { score: number; category: ResilienceResult['category'] }) {
  const styles = CATEGORY_STYLES[category];
  const radius = 54;
  const circumference = 2 * Math.PI * radius;
  // Arc covers 270° (¾ of circle), starting at 135° from top
  const arcLength = circumference * 0.75;
  const filled = arcLength * (score / 100);
  const dashOffset = arcLength - filled;

  return (
    <div className={`relative flex items-center justify-center w-40 h-40 rounded-full ${styles.glow}`}>
      <svg className="absolute inset-0 w-full h-full -rotate-[135deg]" viewBox="0 0 128 128">
        {/* Track */}
        <circle
          cx="64" cy="64" r={radius}
          fill="none"
          stroke="rgba(255,255,255,0.06)"
          strokeWidth="10"
          strokeDasharray={`${arcLength} ${circumference}`}
          strokeLinecap="round"
        />
        {/* Fill */}
        <circle
          cx="64" cy="64" r={radius}
          fill="none"
          className={styles.ring}
          strokeWidth="10"
          strokeDasharray={`${filled} ${circumference}`}
          strokeDashoffset={-dashOffset + arcLength - filled}
          strokeLinecap="round"
          style={{ transition: 'stroke-dasharray 0.6s ease' }}
        />
      </svg>
      <div className="text-center z-10">
        <p className="text-4xl font-bold text-white tabular-nums">{score}</p>
        <p className="text-xs text-zinc-500 mt-0.5">/ 100</p>
      </div>
    </div>
  );
}

function ResilienceSectionSkeleton() {
  return (
    <div className="space-y-6">
      <div className="flex flex-col items-center gap-4 py-6">
        <div className="w-40 h-40 rounded-full bg-zinc-800 animate-pulse" />
        <div className="h-6 w-32 bg-zinc-800 rounded animate-pulse" />
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[1, 2, 3, 4].map((i) => (
          <div key={i} className="p-4 border border-zinc-800 rounded-lg bg-zinc-900/30">
            <div className="h-5 w-5 bg-zinc-800 rounded animate-pulse mb-3" />
            <div className="h-7 w-20 bg-zinc-800 rounded animate-pulse mb-1" />
            <div className="h-4 w-24 bg-zinc-800/50 rounded animate-pulse" />
          </div>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface ResilienceSectionProps {
  userId: string;
}

export function ResilienceSection({ userId }: ResilienceSectionProps) {
  const today = startOfDay(new Date());
  const startDate = subDays(today, 30).toISOString();
  const endDate = today.toISOString();

  const { data: sleepSummaries, isLoading } = useSleepSummaries(userId, {
    start_date: startDate,
    end_date: endDate,
    limit: 100,
    sort_order: 'desc',
  });

  // Extract the 7 most recent nights that have an HRV reading
  const readings = useMemo(() => {
    const summaries = sleepSummaries?.data ?? [];
    return summaries
      .filter((s) => s.avg_hrv_sdnn_ms !== null && s.avg_hrv_sdnn_ms > 0)
      .slice(0, 7)
      .map((s) => ({ date: s.date, hrv: s.avg_hrv_sdnn_ms as number }))
      .reverse(); // chronological for the chart
  }, [sleepSummaries]);

  const result = useMemo(() => computeResilience(readings), [readings]);

  const chartData = useMemo(
    () =>
      readings.map((r) => ({
        date: format(new Date(r.date), 'MMM d'),
        hrv: Math.round(r.hrv),
      })),
    [readings]
  );

  const styles = result ? CATEGORY_STYLES[result.category] : CATEGORY_STYLES['Normal'];

  return (
    <div className="space-y-6">
      <div className="bg-zinc-900/50 border border-zinc-800 rounded-xl overflow-hidden">
        <SectionHeader title="HRV Resilience Score" />

        <div className="p-6">
          {isLoading ? (
            <ResilienceSectionSkeleton />
          ) : !result ? (
            <div className="text-center py-12 space-y-2">
              <p className="text-zinc-400 font-medium">Insufficient HRV data</p>
              <p className="text-sm text-zinc-500">
                At least {RESILIENCE_CONFIG.minNights} nights with HRV readings are required.
                Sync more sleep data to calculate your resilience score.
              </p>
            </div>
          ) : (
            <div className="space-y-8">
              {/* Score dial + category */}
              <div className="flex flex-col items-center gap-4">
                <ScoreDial score={result.score} category={result.category} />
                <span
                  className={`px-3 py-1 rounded-full text-xs font-medium border ${styles.badge}`}
                >
                  {result.category}
                </span>
                <p className="text-xs text-zinc-500 text-center max-w-xs">
                  Based on the coefficient of variation of your nocturnal HRV
                  over the last {result.nightsUsed} nights.
                </p>
              </div>

              {/* Metric cards */}
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <MetricCard
                  icon={ShieldCheck}
                  iconColor="text-violet-400"
                  iconBgColor="bg-violet-500/10"
                  value={String(result.score)}
                  label="Resilience Score"
                />
                <MetricCard
                  icon={Waves}
                  iconColor="text-amber-400"
                  iconBgColor="bg-amber-500/10"
                  value={`${result.cvPct}%`}
                  label="HRV-CV"
                />
                <MetricCard
                  icon={Heart}
                  iconColor="text-rose-400"
                  iconBgColor="bg-rose-500/10"
                  value={`${result.avgHrv} ms`}
                  label="Avg Nightly HRV"
                />
                <MetricCard
                  icon={CalendarDays}
                  iconColor="text-sky-400"
                  iconBgColor="bg-sky-500/10"
                  value={String(result.nightsUsed)}
                  label="Nights Used"
                />
              </div>

              {/* Nightly HRV chart */}
              {chartData.length > 1 && (
                <div className="pt-4 border-t border-zinc-800">
                  <h4 className="text-sm font-medium text-white mb-4">
                    Nightly HRV (last {result.nightsUsed} nights)
                  </h4>
                  <ChartContainer
                    config={{ hrv: { label: 'HRV (ms)', color: styles.bar } }}
                    className="h-[180px] w-full"
                  >
                    <BarChart accessibilityLayer data={chartData}>
                      <CartesianGrid vertical={false} strokeDasharray="3 3" />
                      <XAxis
                        dataKey="date"
                        tickLine={false}
                        axisLine={false}
                        tickMargin={8}
                        tick={{ fill: '#71717a', fontSize: 11 }}
                      />
                      <YAxis
                        tickLine={false}
                        axisLine={false}
                        tickMargin={8}
                        tick={{ fill: '#71717a', fontSize: 11 }}
                        width={40}
                        unit=" ms"
                      />
                      <ChartTooltip
                        cursor={false}
                        content={<ChartTooltipContent />}
                      />
                      <Bar
                        dataKey="hrv"
                        fill="var(--color-hrv)"
                        radius={[4, 4, 0, 0]}
                      />
                    </BarChart>
                  </ChartContainer>
                </div>
              )}

              {/* Score scale legend */}
              <div className="pt-4 border-t border-zinc-800">
                <h4 className="text-xs font-medium text-zinc-400 mb-3 uppercase tracking-wider">
                  Score Scale (HRV-CV thresholds)
                </h4>
                <div className="space-y-2">
                  {(
                    [
                      { label: 'Elite Stability', range: 'CV ≤ 7%', color: 'bg-emerald-500', score: '100' },
                      { label: 'Normal', range: 'CV 7 – 25%', color: 'bg-sky-500', score: '40 – 99' },
                      { label: 'Volatile', range: 'CV > 25%', color: 'bg-rose-500', score: '0 – 39' },
                    ] as const
                  ).map(({ label, range, color, score }) => (
                    <div key={label} className="flex items-center gap-3">
                      <div className={`w-2.5 h-2.5 rounded-full ${color} shrink-0`} />
                      <span className="text-xs text-zinc-300 w-28 shrink-0">{label}</span>
                      <span className="text-xs text-zinc-500 flex-1">{range}</span>
                      <span className="text-xs font-medium text-white">{score} pts</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
