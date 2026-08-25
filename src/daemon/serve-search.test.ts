import { describe, it, expect, vi } from "vitest";
import { handleSearch, handleApi } from "./serve";
import type { TorrentResult } from "../sources/types";

// Deterministic fake of cachedSearch's underlying source.search path: we
// monkey-patch the service module's SOURCES via the registry is complex;
// instead we test handleSearch's contract by stubbing searchAll through
// module mocking would need vi.mock hoisting — simpler to assert on the
// auth/validation shell and use a tiny in-file source set.
vi.mock("../sources/registry", () => {
  const mk = (id: string, results: () => Promise<TorrentResult[]>) => ({
    id: id as never,
    label: id,
    groups: [],
    homepage: "https://example.com",
    reportsHealth: true,
    search: results,
  });
  return {
    SOURCES: [
      mk("good", async () => [
        {
          infoHash: "a".repeat(40),
          name: "same torrent",
          sizeBytes: 100,
          seeders: 5,
          leechers: 1,
          source: "good" as never,
          magnet: `magnet:?xt=urn:btih:${"a".repeat(40)}`,
        },
        {
          infoHash: "b".repeat(40),
          name: "slow source find",
          sizeBytes: 200,
          seeders: 2,
          leechers: 0,
          source: "good" as never,
          magnet: `magnet:?xt=urn:btih:${"b".repeat(40)}`,
        },
      ]),
      // duplicate infoHash with MORE seeders -> dedupe keeps this one
      mk("dup", async () => [
        {
          infoHash: "a".repeat(40),
          name: "same torrent",
          sizeBytes: 100,
          seeders: 9,
          leechers: 0,
          source: "dup" as never,
          magnet: `magnet:?xt=urn:btih:${"a".repeat(40)}`,
        },
      ]),
      // always throws -> must not fail the whole search
      mk("broken", async () => {
        throw new Error("site down");
      }),
    ],
    DEFAULT_SOURCE: null,
    getSource: () => null,
    sourcesByGroup: () => [],
  };
});

vi.mock("../sources/cache", () => ({
  // bypass TTL cache; call straight through
  cachedSearch: (source: { search: (q: string) => Promise<TorrentResult[]> }, q: string) =>
    source.search(q),
}));

describe("handleSearch", () => {
  it("requires authorization", async () => {
    const res = await handleSearch("s3cret", undefined, new URLSearchParams("q=test"));
    expect(res.status).toBe(401);
  });

  it("rejects a missing q", async () => {
    const res = await handleSearch("s3cret", "Bearer s3cret", new URLSearchParams(""));
    expect(res.status).toBe(400);
  });

  it("dedupes across sources, orders healthiest first, reports per-source errors", async () => {
    const res = await handleSearch(
      "s3cret",
      "Bearer s3cret",
      new URLSearchParams("q=demo"),
    );
    expect(res.status).toBe(200);
    const body = res.body as Record<string, unknown>;
    expect(body.ok).toBe(true);
    expect(body.query).toBe("demo");
    const results = body.results as Array<Record<string, unknown>>;
    // two unique info hashes; hash 'a' kept from 'dup' (9 seeders > 5)
    expect(results).toHaveLength(2);
    expect(results[0]!.infoHash).toBe("a".repeat(40));
    expect((results[0]!.seeders as number)).toBe(9);
    expect(results[1]!.infoHash).toBe("b".repeat(40));
    // per-source bookkeeping
    const sources = body.sources as Record<string, { count: number; error: string | null }>;
    expect(sources.good!.count).toBe(2);
    expect(sources.dup!.count).toBe(1);
    expect(sources.broken!.error).toContain("site down");
    expect(body.timedOut).toBe(false);
  });

  it("handleApi still answers 401/500 for /search (real routing happens in runServe)", async () => {
    const res = await handleApi({ queue: {} } as never, "tok", "GET", "/search", "Bearer tok", "");
    // runServe routes /search before handleApi; the in-handler guard just
    // returns a deterministic error if ever reached with valid auth.
    expect([401, 500]).toContain(res.status);
  });
});
