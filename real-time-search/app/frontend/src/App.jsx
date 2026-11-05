import { useEffect, useLayoutEffect, useRef, useState } from "react";

const L2_STATUS = {
  not_deployed: "L2 not deployed: showing the L1 order",
  ok: "Reranked with live session features",
  timeout: "L2 timed out: showing the L1 order",
  error: "L2 failed: showing the L1 order",
};

// How L2 moved an item vs the L1 column: "new" if it came from below L1's shown top `shown`.
function rankChange(c, position, shown) {
  if (c.l1_rank === undefined) return null;
  if (c.l1_rank > shown) return { kind: "new", label: "NEW" };
  const delta = c.l1_rank - position;
  if (delta > 0) return { kind: "up", label: `▲${delta}` };
  if (delta < 0) return { kind: "down", label: `▼${-delta}` };
  return { kind: "same", label: "" };
}

function diffSummary(changes) {
  const moved = changes.filter((ch) => ch?.kind === "up" || ch?.kind === "down").length;
  const added = changes.filter((ch) => ch?.kind === "new").length;
  if (!moved && !added) return "L2 kept the L1 order";
  return `L2 moved ${moved} of ${changes.length}` + (added ? `, brought in ${added} from below L1's top ${changes.length}` : "");
}

// Which of the scenario's planted intent a completion matches (its top category and most-clicked brand).
function intentMatches(c, intent) {
  if (!intent) return [];
  const hits = [];
  if (intent.brand && c.brand?.toLowerCase() === intent.brand.toLowerCase()) hits.push(c.brand);
  if (intent.category && c.category === intent.category) hits.push(c.category);
  return hits;
}

const EVENT_LABEL = { search: "search", click: "click", add_to_cart: "cart" };

// Bar segments as % of `scale`, clipped at full width (▸ marks the overflow); `marker` draws a target line.
function Bar({ segments, scale, marker, markerLabel }) {
  let used = 0;
  const widths = segments.map((g) => {
    const w = Math.min((100 * g.ms) / scale, 100 - used);
    used += w;
    return w;
  });
  const overflow = segments.reduce((a, g) => a + g.ms, 0) > scale;
  return (
    <div className="bar">
      {segments.map((g, i) => (
        <span key={g.key} className={`seg ${g.key}`} style={{ width: `${widths[i]}%` }} title={`${g.label} ${g.ms} ms`} />
      ))}
      {overflow && <span className="overflow" title={`past ${scale} ms`}>▸</span>}
      {marker !== undefined && (
        <span className="marker" style={{ left: `${(100 * marker) / scale}%` }}>
          <span>{markerLabel}</span>
        </span>
      )}
    </div>
  );
}

const LATE_POLL_MS = 40;
const LATE_GIVE_UP_MS = 2000;

// After a timeout, the L2 call keeps running on the server; fetch its full time to show how far past the budget it ran.
function useL2FullTime(requestId, status) {
  const [full, setFull] = useState(null);
  useEffect(() => {
    setFull(null);
    if (status !== "timeout" || !requestId) return;
    let stop = false;
    const started = Date.now();
    (async () => {
      while (!stop && Date.now() - started < LATE_GIVE_UP_MS) {
        const r = await fetch(`/api/qac/${requestId}/l2_time`).catch(() => null);
        if (stop) return;
        if (r?.ok) return setFull((await r.json()).l2_full_ms);
        await new Promise((res) => setTimeout(res, LATE_POLL_MS));
      }
    })();
    return () => {
      stop = true;
    };
  }, [requestId, status]);
  return full;
}

const round1 = (ms) => Math.round(ms * 10) / 10;

// Fixed keystroke axis (~120 ms measured at the 20 ms debounce), so bar lengths compare across keystrokes. Slower
// keystrokes clip at full width with the real ms in the rows.
const SCREEN_SCALE_MS = 160;
const STAGE_KEYS = ["fs-kafka", "fs-rtm", "fs-lakebase", "fs-read"];

// A bar with its total, then one row per segment: colour key, name, where the time comes from, ms.
function Breakdown({ title, measures, segments, rows, total, totalClass, scale, budget, markerLabel, footer }) {
  return (
    <section className="hood-bar">
      <h3>
        {title}
        <span className={totalClass}>{total}</span>
      </h3>
      {measures && <p className="measures">{measures}</p>}
      <Bar segments={segments} scale={scale} marker={budget} markerLabel={markerLabel} />
      <ul className="breakdown">
        {rows.map((r) => (
          <li key={r.key}>
            <span className={`key ${r.key}`} />
            <span className="name">
              {r.name}
              {r.source && <span className="source">{r.source}</span>}
            </span>
            <span className="ms">{r.ms}</span>
          </li>
        ))}
      </ul>
      {footer && <p className="legend">{footer}</p>}
    </section>
  );
}

// Rows are fixed and shown before there is anything to time, so filling them in never moves the layout.
const STAGES = [
  ["Kafka write", "app"],
  ["Real-Time Mode read + computed", "pipeline · output_time"],
  ["Lakebase write", "pipeline · commit_time"],
  ["App read every feature", "app · Lakebase"],
];
const EMPTY = "–";

function ClickstreamBar({ session, freshness, scale }) {
  const ready = freshness?.status === "ready";
  const footer = !session
    ? "Play a clickstream to time how fast its events reach the session features."
    : ready
      ? "From the last replayed click (sent alone) until the app read every feature. App and pipeline clocks differ by a few ms; no step shows below 0."
      : freshness?.status === "error"
        ? <span className="over">Could not read the online store: {freshness.detail}</span>
        : freshness?.status === "timeout"
          ? <span className="over">Not every feature caught up within {freshness.waited_s} s (landed: {freshness.landed.join(", ") || "none"}). Is the session pipeline running?</span>
          : "Waiting for every session feature to reflect the replayed events…";
  return (
    <Breakdown
      title="Clickstream → features"
      segments={ready ? freshness.stages.map((g, i) => ({ key: STAGE_KEYS[i], label: g.name, ms: Math.max(g.ms, 0) })) : []}
      rows={STAGES.map(([name, source], i) => ({ key: STAGE_KEYS[i], name, source,
                                                 ms: ready ? `${Math.round(freshness.stages[i].ms)} ms` : EMPTY }))}
      total={ready ? `${Math.round(freshness.total_ms)} ms` : EMPTY}
      scale={scale}
      footer={footer}
    />
  );
}

// What the user waits for: from the keystroke event until its completions are painted. Browser clocks, except L1 and
// L2, which are the response's own timings; "network + app" is the rest of the round trip (proxy, TLS, FastAPI, JSON).
// L2 is hatched when it didn't kick in (timeout or error). After a timeout the L1 order is already on screen, so L2's
// overrun is text only.
function ScreenBar({ e2e }) {
  const l2Full = useL2FullTime(e2e?.request_id, e2e?.status);
  const status = e2e?.status;
  const hasL2 = e2e?.l2 != null; // on a timeout, the time until the request gave up on it
  const l2Key = status === "timeout" || status === "error" ? "l2-miss" : "l2";
  const l2Source = status === "error" ? "model serving · error"
    : status === "timeout" ? (l2Full > e2e.l2 ? `finished at ${l2Full} ms; L1 order shown` : "timed out; L1 order shown")
    : "model serving";
  const parts = [
    { key: "e2e-debounce", name: "Debounce", source: "browser waits for a pause", ms: e2e?.debounce },
    { key: "e2e-network", name: "Network + app", source: "round trip minus server time", ms: e2e?.network },
    { key: "l1", name: "L1 lookup", source: e2e ? `Lakebase prefix “${e2e.prefix}”` : "Lakebase prefix table",
      ms: e2e?.l1 },
    { key: hasL2 ? l2Key : "l2", name: status === "timeout" ? "L2 rerank, until the timeout" : "L2 rerank",
      source: l2Source, ms: hasL2 ? e2e.l2 : undefined },
    { key: "e2e-render", name: "Render", source: "response → painted frame", ms: e2e?.render },
  ];
  return (
    <Breakdown
      title="Query Auto-Completion (QAC)"
      measures="Keystroke → on screen"
      segments={e2e ? parts.filter((g) => g.ms !== undefined).map((g) => ({ key: g.key, label: g.name, ms: round1(g.ms) })) : []}
      rows={parts.map((g) => ({ key: g.key, name: g.name, source: g.source,
                                ms: g.ms !== undefined ? `${round1(g.ms)} ms` : EMPTY }))}
      total={e2e ? `${round1(e2e.total)} ms` : EMPTY}
      scale={SCREEN_SCALE_MS}
    />
  );
}

// From Enter (or a picked completion) until the results are painted: no debounce. Embed, search and L2 are the
// response's own timings. The footer compares L1 alone with L1 + L2 server-side, the budget's measure.
const SERP_SCALE_MS = 600;

function SerpBar({ e2e, budget }) {
  const status = e2e?.status;
  const hasL2 = e2e?.l2 != null;
  const l1 = e2e ? e2e.embed + e2e.search : undefined;
  const parts = [
    { key: "e2e-network", name: "Network + app", source: "round trip minus server time", ms: e2e?.network },
    { key: "serp-embed", name: "Query embedding (L1)",
      source: e2e?.embedding === "fallback_managed" ? "pay-per-token fallback" : "rts-query-embedding", ms: e2e?.embed },
    { key: "l1", name: "Hybrid search (L1)", source: e2e ? `AI Search, ${e2e.hits} hits` : "AI Search", ms: e2e?.search },
    { key: hasL2 && (status === "timeout" || status === "error") ? "l2-miss" : "l2",
      name: status === "timeout" ? "L2 rerank, until the timeout" : "L2 rerank",
      source: status === "timeout" ? "timed out; L1 order shown" : status === "error" ? "model serving · error"
        : e2e ? `DCNv2, ${e2e.hits} candidates` : "DCNv2", ms: hasL2 ? e2e.l2 : undefined },
    { key: "e2e-render", name: "Render", source: "response → painted frame", ms: e2e?.render },
  ];
  const over = (ms) => (budget && ms > budget ? "over" : undefined);
  const footer = !e2e ? "Press Enter or pick a completion to time a search." : (
    <>
      Server-side: L1 <span className={over(l1)}>{round1(l1)} ms</span>
      {hasL2 && <> · L1 + L2 <span className={over(l1 + e2e.l2)}>{round1(l1 + e2e.l2)} ms</span></>}
      {budget && <> (budget {budget} ms p95)</>}
    </>
  );
  return (
    <Breakdown
      title="Search Engine Results Page (SERP)"
      measures="Search → results on screen"
      segments={e2e ? parts.filter((g) => g.ms !== undefined).map((g) => ({ key: g.key, label: g.name, ms: round1(g.ms) })) : []}
      rows={parts.map((g) => ({ key: g.key, name: g.name, source: g.source,
                                ms: g.ms !== undefined ? `${round1(g.ms)} ms` : EMPTY }))}
      total={e2e ? `${round1(e2e.total)} ms` : EMPTY}
      scale={SERP_SCALE_MS}
      footer={footer}
    />
  );
}

// The side panel: how fast the replayed clicks reached the features and how fast a keystroke reaches the screen, then
// what was replayed. The keystroke bar has a fixed axis; the clickstream bar fits its total.
function UnderTheHood({ session, result, freshness, e2e, serpE2e, serpBudget,
                       scenarios, scenarioId, onScenario, onPlay, playing, onTry }) {
  const typed = result?.prefix?.trim(); // the empty box is not a keystroke to time
  const freshScale = freshness?.status === "ready" ? Math.max(freshness.total_ms, 1) : 1;
  return (
    <section className="hood" id="hood">
      <h2>Under the hood</h2>
      {/* Demo controls live here, so the storefront shows only the logo and the search bar */}
      <section className="replay">
        <label>
          Shopper replay
          <select value={scenarioId} onChange={(e) => onScenario(e.target.value)}>
            {scenarios.map((s) => (
              <option key={s.scenario_id} value={s.scenario_id}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
        <button onClick={onPlay} disabled={!scenarioId || playing}>
          {playing ? "Replaying…" : "Play clickstream"}
        </button>
        {session && (
          <p className="session">
            Replayed {session.events} events to Kafka as <code>{session.session_id}</code>
            {session.user_id ? ` (signed in, user ${session.user_id})` : " (guest)"}. Try{" "}
            <button className="link" onClick={onTry}>
              “{session.next_query}”
            </button>
          </p>
        )}
      </section>
      <ClickstreamBar session={session} freshness={freshness} scale={freshScale} />
      <ScreenBar e2e={typed ? e2e : null} />
      <SerpBar e2e={serpE2e} budget={serpBudget} />
      {session && <Clickstream session={session} />}
    </section>
  );
}

// After Play, the server waits until every session table reflects the replayed events, then returns the timings.
function useFreshness(session) {
  const [f, setF] = useState(null);
  useEffect(() => {
    setF(null);
    if (!session) return;
    let stop = false;
    fetch(`/api/sessions/${session.session_id}/freshness`)
      .then(async (r) =>
        r.ok ? r.json() : { status: "error", detail: (await r.json().catch(() => ({}))).detail ?? `HTTP ${r.status}` },
      )
      .catch((e) => ({ status: "error", detail: e.message }))
      .then((res) => !stop && setF(res));
    return () => {
      stop = true;
    };
  }, [session?.session_id]);
  return f;
}

const PRICE = /price/;

function formatValue(feature, v) {
  if (v === null || v === undefined) return "–";
  if (PRICE.test(feature) && typeof v === "number") return `$${v.toFixed(2)}`;
  return String(v);
}

function Clickstream({ session }) {
  const { intent, timeline } = session;
  return (
    <>
      <p className="intent">
        Planted intent: <strong>{[intent.category, intent.brand, intent.price_target && `~$${Math.round(intent.price_target)}`]
          .filter(Boolean).join(" · ")}</strong>
        . Expect the session-aware column to lift completions that match it.
      </p>
      <h3>Clickstream history</h3>
      <ol className="events">
        <li className="event next">
          <span className="t">next</span>
          <span className="kind">search</span>
          <span>“{session.next_query}”: type its first letters in the search bar</span>
        </li>
        {/* "back" events (returning to the results) carry no product, so they only add noise here */}
        {timeline.filter((e) => e.event_type !== "back").reverse().map((e, i) => (
          <li key={i} className={`event ${e.event_type}`}>
            <span className="t">{e.t_s === 0 ? "0 s" : `${e.t_s} s`}</span>
            <span className="kind">{EVENT_LABEL[e.event_type] ?? e.event_type}</span>
            <span>
              {e.event_type === "search"
                ? `“${e.query}”`
                : [e.brand, e.category, e.price != null && `$${Math.round(e.price)}`].filter(Boolean).join(" · ")}
            </span>
          </li>
        ))}
      </ol>
    </>
  );
}

// What the L2 endpoint receives, by source; widths are feature counts. Hover a source for its first values.
const SOURCES = [
  ["stream", "Streaming", "Kafka → Real-Time Mode → Lakebase"],
  ["batch", "Batch", "Delta → hourly → Lakebase"],
  ["request", "Request", "the L1 row the app sends"],
];
const TIP_ROWS = 3;

function shortValue(feature, v) {
  return typeof v === "number" && !Number.isInteger(v) && !PRICE.test(feature) ? v.toFixed(2) : formatValue(feature, v);
}

// Rows per source: [feature, value, when the app read it]. Session and user values are the app's own Lakebase reads
// of the tables the endpoint looks up; request values are exactly what was sent for the top completion.
function inputRows(inputs, { freshness, userFeatures, top, session, userId }) {
  const ready = freshness?.status === "ready";
  return {
    stream: ready
      ? freshness.features.map((x) => [x.feature, x.value, x.served_ms === null ? "earlier event" : `+${Math.round(x.served_ms)} ms`])
      : inputs.stream.map((f) => [f, null, session ? "waiting" : "no session"]),
    batch: (userFeatures ?? inputs.batch.map((f) => ({ feature: f, value: null }))).map((x) => [x.feature, x.value, "hourly batch"]),
    request: inputs.request.map((f) => [
      f,
      !top ? null : f === "l1_rank" ? top.rank : f === "is_guest" ? Number(!userId) : top[f],
      "in request",
    ]),
  };
}

function FeatureBar({ inputs, freshness, userFeatures, top, session, userId }) {
  if (!inputs) return null;
  const rows = inputRows(inputs, { freshness, userFeatures, top, session, userId });
  const guest = !userId;
  return (
    <span className="fbar">
      {SOURCES.map(([key, label, path]) => {
        const off = key === "batch" && guest;
        return (
          <span key={key} className={`fseg ${key}${off ? " off" : ""}`} style={{ flexGrow: inputs[key].length }} tabIndex={0}>
            <span className="ftip">
              <strong>{label} · {inputs[key].length} features</strong>
              <span className="fpath">{key === "request" && top ? `“${top.completion}”: ${path}` : path}</span>
              {off ? (
                <span className="fnote">Guest: no user history, so these are empty</span>
              ) : (
                <>
                  {rows[key].slice(0, TIP_ROWS).map(([f, v, at]) => (
                    <span key={f} className="frow">
                      <code>{f}</code>
                      <span>{shortValue(f, v)}</span>
                      <span className="fat">{at}</span>
                    </span>
                  ))}
                  {rows[key].length > TIP_ROWS && <span className="fmore">… {rows[key].length - TIP_ROWS} more</span>}
                </>
              )}
            </span>
          </span>
        );
      })}
    </span>
  );
}

// The signed-in user's batch features, read once per sign-in (they only change hourly).
function useUserFeatures(userId) {
  const [features, setFeatures] = useState(null);
  useEffect(() => {
    setFeatures(null);
    if (!userId) return;
    fetch(`/api/users/${userId}/features`)
      .then((r) => (r.ok ? r.json() : null))
      .then((res) => res && setFeatures(res.features));
  }, [userId]);
  return features;
}

// Store-style account menu: sign in as one of data's demo personas, or continue as a guest. Who you are is the sign-in
// (long-term user features); what you just did is the replayed clickstream (session features). They combine freely.
function Avatar({ name }) {
  return <span className="avatar" aria-hidden>{name.split(" ").map((w) => w[0]).join("")}</span>;
}

function SignIn({ personas, me, onPick }) {
  const [open, setOpen] = useState(false);
  const choose = (p) => {
    onPick(p);
    setOpen(false);
  };
  return (
    <div className="signin" onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false); }}
      onKeyDown={(e) => e.key === "Escape" && setOpen(false)}>
      <button className="signin-button" aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((o) => !o)}>
        {me ? <><Avatar name={me.name} />Hi, {me.name.split(" ")[0]} ▾</> : "Sign in"}
      </button>
      {open && (
        <div className="signin-menu" role="dialog" aria-label="Sign in">
          <p className="signin-title">{me ? "Switch account" : "Sign in to Brick Lane Goods"}</p>
          {personas.map((p) => (
            <button key={p.persona_id} className="persona" aria-pressed={me?.persona_id === p.persona_id}
              onClick={() => choose(p)}>
              <Avatar name={p.name} />
              <span>
                <strong>{p.name}</strong>
                <span className="persona-meta">
                  Shops {p.category} · {p.category_events_30d} events in 30 days · ~${Math.round(p.median_price_30d)}
                </span>
              </span>
            </button>
          ))}
          <button className="link guest" onClick={() => choose(null)}>
            {me ? "Sign out and continue as guest" : "Continue as guest"}
          </button>
        </div>
      )}
    </div>
  );
}

// Retail autocomplete style: the typed prefix stays plain and the rest of the completion is bold.
function Completion({ text, prefix }) {
  const p = prefix?.trim() ?? "";
  if (!p || !text.toLowerCase().startsWith(p.toLowerCase())) return <strong>{text}</strong>;
  return <>{text.slice(0, p.length)}<strong>{text.slice(p.length)}</strong></>;
}

// `listId` marks the column ↑/↓ moves through (`active` is its highlighted row); a click on any row picks it.
function Column({ title, subtitle, items, note, shown, intent, onPick, listId, active, prefix }) {
  const changes = items.map((c, i) => (shown === undefined ? null : rankChange(c, i + 1, shown)));
  const diffed = changes.some((ch) => ch);
  return (
    <section className="column">
      {/* One header row shared by both columns (subgrid), so their lists start level */}
      <header>
        <h2>{title}</h2>
        <p className="subtitle">{subtitle}</p>
        {note && <p className="note">{note}</p>}
        {diffed && <p className="summary">{diffSummary(changes)}</p>}
      </header>
      <ol id={listId} role={listId && "listbox"}>
        {items.map((c, i) => (
          <li key={c.completion} id={listId && `${listId}-${i}`} role={listId && "option"}
            aria-selected={listId ? i === active : undefined} onClick={() => onPick(c.completion)}
            title={`${c.pop_30d} searches · ${Math.round(c.ctr * 100)}% engaged`}
            className={[changes[i] && `change-${changes[i].kind}`, listId && i === active && "active"]
              .filter(Boolean).join(" ") || undefined}>
            <span className="glyph" aria-hidden>⌕</span>
            <span className="completion">
              <Completion text={c.completion} prefix={prefix} />
              {changes[i]?.label && <span className={`badge ${changes[i].kind}`}>{changes[i].label}</span>}
              {intentMatches(c, intent).map((m) => (
                <span key={m} className="badge intent">{m}</span>
              ))}
            </span>
            <span className="cat">{c.category}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

// ESCI has no product images, so each tile shows its category's icon (public/categories, in the logo's palette).
// "Clothing, Shoes & Jewelry" -> clothing-shoes-jewelry.svg; an unknown or missing category falls back to other.svg.
const categoryIcon = (category = "") =>
  `/categories/${category.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "other"}.svg`;

// FLIP: after `key` changes, slide each tile (by id) from where it was to where it is now.
function useFlip(ref, key) {
  const prev = useRef(new Map());
  useLayoutEffect(() => {
    const tiles = [...(ref.current?.children ?? [])];
    const next = new Map(tiles.map((el) => [el.dataset.id, el.getBoundingClientRect()]));
    for (const el of tiles) {
      const was = prev.current.get(el.dataset.id);
      const now = next.get(el.dataset.id);
      el.style.transition = "none";
      el.style.transform = was ? `translate(${was.left - now.left}px, ${was.top - now.top}px)` : "scale(0.96)";
      el.style.opacity = was ? "" : "0";
    }
    const frame = requestAnimationFrame(() => {
      for (const el of tiles) {
        el.style.transition = "transform 250ms ease-out, opacity 250ms ease-out";
        el.style.transform = "";
        el.style.opacity = "";
      }
    });
    prev.current = next;
    return () => cancelAnimationFrame(frame);
  }, [key]);
}

const SERP_VIEWS = {
  l1: ["Hybrid L1", "AI Search: our query vector + keyword match on title and brand"],
  l2: ["L1 + L2", "DCNv2 reranks all L1 hits with session, user and item features"],
};

const PAGE_SIZE = 10;
const PAGES = 4; // the server sends 4 pages; the "…" after them is decoration

// A move up from another L1 page says which, so L2 lifting an item from deep in L1 shows.
function pagedChange(ch, l1Rank, page) {
  const from = Math.ceil(l1Rank / PAGE_SIZE);
  return ch?.kind === "up" && from !== page + 1 ? { ...ch, label: `${ch.label} from p${from}` } : ch;
}

// The SERP: one product grid, toggled between the L1 order and the L2 rerank; L2 shows rank badges vs L1's top N.
// Every page is in the one response, so paging is a slice: no new search, no new L2 call.
function ResultGrid({ serp, intent }) {
  const l2ok = !!serp.l2.items;
  const [picked, setPicked] = useState("l2");
  const [paged, setPaged] = useState({ serp, page: 0 });
  const page = paged.serp === serp ? paged.page : 0; // a new search starts on page 1
  const view = l2ok ? picked : "l1";
  const all = view === "l2" ? serp.l2.items : serp.l1;
  const allChanges = all.map((h, i) => (view === "l2" ? rankChange(h, i + 1, serp.l1.length) : null));
  const pages = Math.min(PAGES, Math.ceil(all.length / PAGE_SIZE));
  const start = page * PAGE_SIZE;
  const items = all.slice(start, start + PAGE_SIZE);
  const changes = allChanges.slice(start, start + PAGE_SIZE).map((ch, i) => pagedChange(ch, items[i].l1_rank, page));
  const goTo = (p) => setPaged({ serp, page: p });
  const grid = useRef(null);
  useFlip(grid, `${serp.query}\n${view}\n${page}`);
  const note = L2_STATUS[serp.l2.status] ?? serp.l2.status;
  return (
    <>
      <div className="serp-head">
        <div className="toggle" role="group" aria-label="Ranking">
          {Object.entries(SERP_VIEWS).map(([key, [label]]) => (
            <button key={key} aria-pressed={view === key} onClick={() => setPicked(key)}
              disabled={key === "l2" && !l2ok} title={key === "l2" && !l2ok ? note : undefined}>
              {label}
            </button>
          ))}
        </div>
        <h2>{serp.hits} results for “{serp.query}”</h2>
      </div>
      <p className="subtitle">
        {SERP_VIEWS[view][1]}
        {view === "l2" && <> · <span className="summary">{diffSummary(allChanges)}</span></>}
      </p>
      {!l2ok && <p className="note">{note}</p>}
      <ol className="grid" ref={grid}>
        {items.map((h, i) => (
          <li key={h.product_id} data-id={h.product_id}
            className={["tile", changes[i] && `change-${changes[i].kind}`].filter(Boolean).join(" ")}>
            <span className="thumb" title={h.category}>
              <img src={categoryIcon(h.category ?? "")} alt={h.category ?? ""}
                onError={(e) => { e.currentTarget.onerror = null; e.currentTarget.src = "/categories/other.svg"; }} />
            </span>
            {changes[i]?.label && <span className={`badge rank ${changes[i].kind}`} title={`L1 #${h.l1_rank}`}>
              {changes[i].label}</span>}
            <span className="ttitle" title={h.title}>{h.title}</span>
            {h.brand && <span className="brand">{h.brand}</span>}
            {h.price != null && <span className="price">${Number(h.price).toFixed(2)}</span>}
            <span className="chips">
              {intentMatches(h, intent).map((m) => (
                <span key={m} className="badge intent">{m}</span>
              ))}
            </span>
          </li>
        ))}
      </ol>
      {pages > 1 && (
        <nav className="pager" aria-label="Result pages">
          <button onClick={() => goTo(page - 1)} disabled={page === 0} aria-label="Previous page">‹</button>
          {Array.from({ length: pages }, (_, p) => (
            <button key={p} onClick={() => goTo(p)} aria-current={p === page ? "page" : undefined}>{p + 1}</button>
          ))}
          <span className="more" aria-hidden>…</span>
          <button onClick={() => goTo(page + 1)} disabled={page === pages - 1} aria-label="Next page">›</button>
          <span className="pager-note">All {pages} pages came back in this one search: paging makes no new call</span>
        </nav>
      )}
    </>
  );
}

const SERP_EMBEDDING = { custom: "our endpoint", fallback_managed: "pay-per-token fallback" };

function SerpTimings({ serp }) {
  const t = serp.timings_ms;
  const server = (t.embed ?? 0) + (t.search ?? 0) + (t.l2 ?? 0);
  return (
    <p className="serp-timings">
      {serp.hits} hits for “{serp.query}” · embed {round1(t.embed)} ms ({SERP_EMBEDDING[serp.embedding] ?? serp.embedding})
      · {serp.query_type.toLowerCase()} search {round1(t.search)} ms
      {t.l2 !== undefined && <> · L2 {round1(t.l2)} ms</>} · <strong>{round1(server)} ms</strong> server-side
    </p>
  );
}

export default function App() {
  const [cfg, setCfg] = useState({ qac_debounce_ms: 80 });
  const [scenarios, setScenarios] = useState([]);
  const [scenarioId, setScenarioId] = useState("");
  const [session, setSession] = useState(null);
  const [personas, setPersonas] = useState([]);
  const [me, setMe] = useState(null); // the signed-in persona; null is a guest
  const userId = me?.user_id ?? null;
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState(null);
  const [q, setQ] = useState("");
  const [result, setResult] = useState(null);
  const [e2e, setE2e] = useState(null);
  const [open, setOpen] = useState(false); // the QAC dropdown
  const [active, setActive] = useState(-1); // its ↑/↓ row in Session-aware, -1 for none
  const [serp, setSerp] = useState(null);
  const [searching, setSearching] = useState(false);
  const [serpE2e, setSerpE2e] = useState(null);
  const [hoodOpen, setHoodOpen] = useState(() => window.innerWidth >= 1440); // open where it fits beside the store (see .page.hood-open)
  const latestSearch = useRef(0);
  const input = useRef(null);
  const latest = useRef(0);
  const keyAt = useRef(null); // event timestamp of the last keystroke (or "Try" click), on performance.now()'s clock

  useEffect(() => {
    fetch("/api/config").then((r) => r.json()).then(setCfg);
    fetch("/api/scenarios")
      .then((r) => r.json())
      .then((s) => {
        setScenarios(s);
        if (s.length) setScenarioId(s[0].scenario_id);
      })
      .catch(() => setError("Could not load scenarios"));
    fetch("/api/personas").then((r) => (r.ok ? r.json() : [])).then(setPersonas);
  }, []);

  useEffect(() => {
    const seq = ++latest.current;
    const pressed = keyAt.current; // null when a new session, not a keystroke, refetched: nothing to time
    keyAt.current = null;
    const t = setTimeout(() => {
      const sent = performance.now();
      const params = new URLSearchParams({ q });
      if (session) params.set("session_id", session.session_id);
      if (userId) params.set("user_id", userId);
      fetch(`/api/qac?${params}`)
        .then((r) => r.json())
        .then((res) => {
          if (seq !== latest.current) return; // drop responses to older keystrokes
          setResult({ ...res, clock: pressed === null ? null : { pressed, sent, received: performance.now() } });
        });
    }, cfg.qac_debounce_ms);
    return () => clearTimeout(t);
  }, [q, session, userId, cfg.qac_debounce_ms]);

  async function play() {
    setPlaying(true);
    setError(null);
    try {
      const who = userId ? `?${new URLSearchParams({ user_id: userId })}` : ""; // replayed as whoever is signed in
      const r = await fetch(`/api/scenarios/${scenarioId}/play${who}`, { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      setSession(await r.json());
    } catch (e) {
      setError(`Replay failed: ${e.message}`);
    } finally {
      setPlaying(false);
    }
  }

  useEffect(() => {
    const c = result?.clock;
    if (!c) return;
    let frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => {
        const painted = performance.now();
        const t = result.timings_ms;
        const server = t.l1 + (t.l2 ?? 0);
        const debounce = c.sent - c.pressed;
        setE2e({ debounce, network: Math.max(0, c.received - c.sent - server), server,
                 render: painted - c.received, total: painted - c.pressed,
                 l1: t.l1, l2: t.l2, status: result.l2?.status, request_id: result.request_id, prefix: result.prefix });
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [result]);

  // Like the keystroke bar: wait two frames so the results are painted before stopping the clock.
  useEffect(() => {
    const c = serp?.clock;
    if (!c) return;
    let frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => {
        const painted = performance.now();
        const t = serp.timings_ms;
        const server = t.embed + t.search + (t.l2 ?? 0);
        setSerpE2e({ network: Math.max(0, c.received - c.sent - server), embed: t.embed, search: t.search, l2: t.l2,
                     render: painted - c.received, total: painted - c.sent, status: serp.l2?.status,
                     hits: serp.hits, embedding: serp.embedding });
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [serp]);

  const freshness = useFreshness(session);
  const userFeatures = useUserFeatures(userId);
  const l1 = result?.l1 ?? [];
  const l2 = result?.l2 ?? { status: "not_deployed" };
  const l2Items = l2.items ?? l1;
  const listed = open && l2Items.length > 0;

  // Searches with the current session, so L2 sees the same features QAC did; a newer search drops older responses.
  function runSearch(text) {
    if (!text.trim()) return;
    const seq = ++latestSearch.current;
    const params = new URLSearchParams({ q: text });
    if (session) params.set("session_id", session.session_id);
    if (userId) params.set("user_id", userId);
    setSearching(true);
    setError(null);
    const sent = performance.now(); // no debounce: the search starts on the Enter or pick itself
    fetch(`/api/search?${params}`)
      .then((r) => (r.ok ? r.json() : r.text().then((t) => Promise.reject(new Error(t)))))
      .then((res) => seq === latestSearch.current && setSerp({ ...res, clock: { sent, received: performance.now() } }))
      .catch((e) => seq === latestSearch.current && setError(`Search failed: ${e.message}`))
      .finally(() => seq === latestSearch.current && setSearching(false));
  }

  // Signing in or out re-runs the current search, as a store's reload after login would (QAC refetches on its own:
  // userId is in its effect). Neither is a keystroke, so neither is timed.
  const signedInOnce = useRef(false);
  useEffect(() => {
    if (!signedInOnce.current) {
      signedInOnce.current = true;
      return;
    }
    if (serp) runSearch(serp.query);
  }, [userId]);

  // Picking a completion fills the box, closes the dropdown and searches (it isn't a keystroke, so it isn't timed).
  function pick(completion) {
    setQ(completion);
    setActive(-1);
    setOpen(false);
    runSearch(completion);
  }

  function onKeyDown(e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); // keep the caret where it is
      setOpen(true);
      setActive((a) => Math.max(-1, Math.min(l2Items.length - 1, a + (e.key === "ArrowDown" ? 1 : -1))));
    } else if (e.key === "Enter" && listed && l2Items[active]) {
      pick(l2Items[active].completion);
    } else if (e.key === "Enter") {
      setOpen(false);
      runSearch(q);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  // The replay's "Try" link types its next query into the box (timed like a keystroke).
  function onTry(e) {
    keyAt.current = e.timeStamp;
    setQ(session.next_query);
    setActive(-1);
    input.current.focus(); // opens the dropdown
  }

  // The logo goes "home": clear the query and results without a reload, so the replayed session stays.
  function goHome(e) {
    e.preventDefault();
    latestSearch.current++; // drop an in-flight search
    keyAt.current = null; // clearing the box is not a keystroke to time
    setQ("");
    setActive(-1);
    setOpen(false);
    setSerp(null);
    setSerpE2e(null);
    setSearching(false);
    setError(null);
  }

  return (
    <div className={hoodOpen ? "page hood-open" : "page"}>
      {/* A drawer on the left edge: on wide screens it pushes the storefront over, on narrow ones it overlays it.
          Hiding it only slides it away: it stays mounted, so its timings and replay are kept. */}
      <aside className={hoodOpen ? "drawer" : "drawer closed"}>
        <button className="drawer-tab" onClick={() => setHoodOpen((o) => !o)} aria-expanded={hoodOpen}
          aria-controls="hood" title={hoodOpen ? "Hide under the hood" : "Show under the hood"}>
          {hoodOpen ? "‹" : "›"}
        </button>
        <UnderTheHood session={session} result={result} freshness={freshness} e2e={e2e} serpE2e={serpE2e}
          serpBudget={cfg.serp_budget_ms} scenarios={scenarios} scenarioId={scenarioId} onScenario={setScenarioId}
          onPlay={play} playing={playing} onTry={onTry} />
      </aside>
      <main>
        <SignIn personas={personas} me={me} onPick={setMe} />
        <h1 className="store">
          <a href="/" onClick={goHome}>
            <img src="/logo.svg" alt="" />
            Brick Lane Goods
          </a>
        </h1>

        {/* Focus leaving the box and its dropdown closes it; mousedown inside the dropdown keeps focus in the box */}
        <div className="searchbox" onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setOpen(false); }}>
          <svg className="search-icon" viewBox="0 0 24 24" aria-hidden>
            <circle cx="10.5" cy="10.5" r="6.5" />
            <line x1="15.5" y1="15.5" x2="21" y2="21" />
          </svg>
          <input
            ref={input}
            className="search"
            autoFocus
            placeholder="Search"
            value={q}
            role="combobox"
            aria-expanded={listed}
            aria-controls="qac-l2"
            aria-activedescendant={listed && active >= 0 ? `qac-l2-${active}` : undefined}
            onFocus={() => setOpen(true)}
            onChange={(e) => { keyAt.current = e.timeStamp; setQ(e.target.value); setActive(-1); setOpen(true); }}
            onKeyDown={onKeyDown}
          />

          {listed && (
            <div className="dropdown" onMouseDown={(e) => e.preventDefault()}>
              <div className="columns">
                <Column title="Static" subtitle="L1: popularity from the prefix table" items={l1} intent={session?.intent}
                  onPick={pick} prefix={result?.prefix} />
                <Column
                  title="Session-aware"
                  subtitle={
                    <>
                      L1 + L2: with features from{" "}
                      <FeatureBar inputs={cfg.l2_inputs} freshness={freshness} userFeatures={userFeatures}
                        top={l2Items[0]} session={session} userId={userId} />
                    </>
                  }
                  items={l2Items}
                  note={L2_STATUS[l2.status] ?? l2.status}
                  shown={l2.items ? l1.length : undefined}
                  intent={session?.intent}
                  onPick={pick}
                  listId="qac-l2"
                  active={active}
                  prefix={result?.prefix}
                />
              </div>
            </div>
          )}
        </div>
        {error && <p className="error">{error}</p>}

        {serp && serp.l1.length > 0 && (
          <section className={`serp${searching ? " stale" : ""}`}>
            <ResultGrid serp={serp} intent={session?.intent} />
            <SerpTimings serp={serp} />
          </section>
        )}
        {serp && serp.l1.length === 0 && <p className="serp-timings">No results for “{serp.query}”</p>}
      </main>
    </div>
  );
}
