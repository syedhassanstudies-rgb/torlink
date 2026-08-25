// Pure search service: fan out to all sources, dedupe, order healthiest
// first. No React/Ink imports — shared by the TUI hook and the headless
// daemon's GET /search endpoint.

import { SOURCES } from "../sources/registry";
import { cachedSearch } from "../sources/cache";
import type { Source, SourceGroup, SourceId, TorrentResult } from "../sources/types";

export interface SourceOutcome {
  id: SourceId;
  count: number;
  error: string | null;
}

export interface SearchAllResult {
  query: string;
  results: TorrentResult[];
  perSource: Record<SourceId, SourceOutcome>;
  elapsedMs: number;
  timedOut: boolean;
}

// torlink's dedupe: one row per info hash, keeping the copy with the most
// seeders (different sites report slightly different swarm counts).
export function dedupe(list: TorrentResult[]): TorrentResult[] {
  const byHash = new Map<string, TorrentResult>();
  for (const r of list) {
    const existing = byHash.get(r.infoHash);
    if (!existing || r.seeders > existing.seeders) byHash.set(r.infoHash, r);
  }
  return [...byHash.values()];
}

// torlink's default ordering: healthiest first, then newest.
export function defaultOrder(list: TorrentResult[]): TorrentResult[] {
  return list.sort((a, b) => {
    if (b.seeders !== a.seeders) return b.seeders - a.seeders;
    return (b.added ?? 0) - (a.added ?? 0);
  });
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timeout after ${ms}ms`)), ms);
    p.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (e: unknown) => {
        clearTimeout(timer);
        reject(e instanceof Error ? e : new Error(String(e)));
      },
    );
  });
}

interface Attempt {
  source: Source;
  promise: Promise<TorrentResult[]>;
}

/**
 * Search every registered source concurrently and wait for all of them.
 *
 * A source that exceeds `sourceTimeoutMs` (or throws) is recorded as an
 * error for that source; it never fails the whole search. The overall
 * call is capped at `totalTimeoutMs`: when the cap fires first, the
 * result carries whatever finished in time plus `timedOut: true`.
 */
export async function searchAll(
  query: string,
  opts: { signal?: AbortSignal; sourceTimeoutMs?: number; totalTimeoutMs?: number } = {},
): Promise<SearchAllResult> {
  const sourceTimeoutMs = opts.sourceTimeoutMs ?? 10_000;
  const totalTimeoutMs = opts.totalTimeoutMs ?? 15_000;
  const startedAt = Date.now();

  const attempts: Attempt[] = SOURCES.map((source) => ({
    source,
    // Catch here so one rejection can't take down allSettled bookkeeping
    // before slower sources land; errors surface as null results below.
    promise: withTimeout(cachedSearch(source, query, { signal: opts.signal }), sourceTimeoutMs).catch(
      (e: unknown): TorrentResult[] | null => {
        if (opts.signal?.aborted) return null; // caller went away: not an error worth reporting per-source
        throw e;
      },
    ) as Promise<TorrentResult[]>,
  }));

  const guard = Promise.all(
    attempts.map(async (a) => {
      try {
        const results = await a.promise;
        return { source: a.source, results, error: null as string | null };
      } catch (e: unknown) {
        return {
          source: a.source,
          results: [] as TorrentResult[],
          error: e instanceof Error ? e.message : String(e),
        };
      }
    }),
  );

  let timedOut = false;
  const settled = await Promise.race([
    guard.then((r) => ({ kind: "done" as const, r })),
    new Promise<{ kind: "timeout" }>((resolve) =>
      setTimeout(() => resolve({ kind: "timeout" }), totalTimeoutMs),
    ),
  ]);

  if (settled.kind === "timeout") {
    timedOut = true;
    return {
      query,
      results: [],
      perSource: Object.fromEntries(
        SOURCES.map((s) => [s.id, { id: s.id, count: 0, error: "total timeout" }]),
      ) as Record<SourceId, SourceOutcome>,
      elapsedMs: Date.now() - startedAt,
      timedOut,
    };
  }

  const collected: TorrentResult[] = [];
  const perSource = {} as Record<SourceId, SourceOutcome>;
  for (const entry of settled.r) {
    collected.push(...entry.results);
    perSource[entry.source.id] = {
      id: entry.source.id,
      count: entry.results.length,
      error: entry.error,
    };
  }

  return {
    query,
    results: defaultOrder(dedupe(collected)),
    perSource,
    elapsedMs: Date.now() - startedAt,
    timedOut,
  };
}

/** Metadata for every registered source (drives /api/search/sources). */
export function searchSources(): {
  id: SourceId;
  label: string;
  groups: SourceGroup[];
  homepage: string;
  reportsHealth: boolean;
}[] {
  return SOURCES.map((s: Source) => ({
    id: s.id,
    label: s.label,
    groups: [...(s.groups ?? [])],
    homepage: s.homepage,
    reportsHealth: s.reportsHealth,
  }));
}
