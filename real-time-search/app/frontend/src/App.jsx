import { useEffect, useRef, useState } from "react";

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

// Fixed keystroke axis: covers the measured p99 end to end (L1 4.8 + L2 36.5-40.7 ms back to back; typing-pace calls
// 32-56 ms), so the 40 ms budget line stays put. Slower keystrokes clip at full width with the real ms in the rows.
const KEYSTROKE_SCALE_MS = 60;
const STAGE_KEYS = ["fs-kafka", "fs-rtm", "fs-lakebase", "fs-read"];

// A bar with its total, then one row per segment: colour key, name, where the time comes from, ms.
function Breakdown({ title, segments, rows, total, totalClass, scale, budget, markerLabel, footer }) {
  return (
    <section className="hood-bar">
      <h3>
        {title}
        <span className={totalClass}>{total}</span>
      </h3>
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

function KeystrokeBar({ result, status, l2Full, scale, budget }) {
  const t = result?.timings_ms;
  const l1 = t?.l1;
  const l2 = t?.l2; // on a timeout, the time until the request gave up on it
  const l2Key = status === "timeout" ? "l2-timeout" : status === "error" ? "l2-error" : "l2";
  const late = l2Full > l2;
  const segments = [];
  if (t) {
    segments.push({ key: "l1", label: "L1", ms: l1 });
    if (l2 !== undefined) segments.push({ key: l2Key, label: "L2", ms: l2 });
    if (late) segments.push({ key: "l2-late", label: "L2 after the timeout", ms: l2Full - l2 });
  }
  const rows = [
    { key: "l1", name: "L1 lookup", source: t ? `Lakebase prefix “${result.prefix}”` : "Lakebase prefix table",
      ms: t ? `${l1} ms` : EMPTY },
    { key: t && l2 !== undefined ? l2Key : "l2", name: status === "timeout" ? "L2 rerank, until the timeout" : "L2 rerank",
      source: status === "error" ? "model serving · error" : "model serving", ms: l2 !== undefined ? `${l2} ms` : EMPTY },
    { key: "l2-late", name: "L2 after the timeout",
      source: late ? `finished at ${l2Full} ms; L1 order shown` : "only when L2 misses the timeout",
      ms: late ? `+${round1(l2Full - l2)} ms` : EMPTY },
  ];
  const served = t ? l1 + (l2 ?? 0) : 0;
  const full = t ? l1 + Math.max(l2 ?? 0, l2Full ?? 0) : 0;
  const over = full - budget;
  return (
    <Breakdown
      title="Keystroke → completions"
      segments={segments}
      rows={rows}
      total={t ? `${round1(full)} ms` : EMPTY}
      totalClass={t && over > 0 ? "over" : undefined}
      scale={scale}
      budget={budget}
      markerLabel={`${budget} ms budget`}
      footer={
        !t ? (
          "Type in the search bar to time each keystroke."
        ) : over > 0 ? (
          <span className="over">
            Served in {round1(served)} ms; L1 + L2 took {round1(full)} ms, {round1(over)} ms over the {budget} ms budget.
          </span>
        ) : (
          `Served in ${round1(served)} ms, ${round1(-over)} ms under the ${budget} ms budget.`
        )
      }
    />
  );
}

// What the user waits for: from the keystroke event until its completions are painted. Browser clocks only; the server
// part is the response's own timings, so "network + app" is the rest of the round trip (proxy, TLS, FastAPI, JSON).
function ScreenBar({ e2e }) {
  const parts = e2e && [
    { key: "e2e-debounce", label: "Debounce", source: "browser waits for a pause", ms: e2e.debounce },
    { key: "e2e-network", label: "Network + app", source: "round trip minus server time", ms: e2e.network },
    { key: "l1", label: "Server (L1 + L2)", source: "timings_ms, as in the bar above", ms: e2e.server },
    { key: "e2e-render", label: "Render", source: "response → painted frame", ms: e2e.render },
  ];
  const rows = (parts ?? [
    { key: "e2e-debounce", label: "Debounce", source: "browser waits for a pause" },
    { key: "e2e-network", label: "Network + app", source: "round trip minus server time" },
    { key: "l1", label: "Server (L1 + L2)", source: "timings_ms, as in the bar above" },
    { key: "e2e-render", label: "Render", source: "response → painted frame" },
  ]).map((g) => ({ key: g.key, name: g.label, source: g.source, ms: parts ? `${round1(g.ms)} ms` : EMPTY }));
  return (
    <Breakdown
      title="Keystroke → on screen"
      segments={parts ?? []}
      rows={rows}
      total={e2e ? `${round1(e2e.total)} ms` : EMPTY}
      scale={e2e ? Math.max(e2e.total, 1) : 1}
      footer={e2e ? "Measured in this browser, last keystroke only; no end-to-end target set yet." : "Type in the search bar to time what you see."}
    />
  );
}

// The side panel: how fast the replayed clicks reached the features and how fast a keystroke is answered, then what
// was replayed. The keystroke bar has a fixed axis so the budget line never moves; the clickstream bar fits its total.
function UnderTheHood({ session, result: qacResult, status, budget, freshness, e2e }) {
  const result = qacResult?.prefix?.trim() ? qacResult : null; // the empty box is not a keystroke to time
  const l2Full = useL2FullTime(result?.request_id, status);
  const freshScale = freshness?.status === "ready" ? Math.max(freshness.total_ms, 1) : 1;
  return (
    <section className="hood">
      <h2>Under the hood</h2>
      <ClickstreamBar session={session} freshness={freshness} scale={freshScale} />
      <KeystrokeBar result={result} status={status} l2Full={l2Full} scale={KEYSTROKE_SCALE_MS} budget={budget} />
      <ScreenBar e2e={result ? e2e : null} />
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
function inputRows(inputs, { freshness, userFeatures, top, session }) {
  const ready = freshness?.status === "ready";
  return {
    stream: ready
      ? freshness.features.map((x) => [x.feature, x.value, x.served_ms === null ? "earlier event" : `+${Math.round(x.served_ms)} ms`])
      : inputs.stream.map((f) => [f, null, session ? "waiting" : "no session"]),
    batch: (userFeatures ?? inputs.batch.map((f) => ({ feature: f, value: null }))).map((x) => [x.feature, x.value, "hourly batch"]),
    request: inputs.request.map((f) => [
      f,
      !top ? null : f === "l1_rank" ? top.rank : f === "is_guest" ? Number(!session?.user_id) : top[f],
      "in request",
    ]),
  };
}

function FeatureBar({ inputs, freshness, userFeatures, top, session }) {
  if (!inputs) return null;
  const rows = inputRows(inputs, { freshness, userFeatures, top, session });
  const guest = session && !session.user_id;
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

// A logged-in session's batch features, read once per Play (they only change hourly).
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

function Column({ title, subtitle, items, note, shown, intent }) {
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
      <ol>
        {items.map((c, i) => (
          <li key={c.completion} className={changes[i] ? `change-${changes[i].kind}` : undefined}>
            <span className="completion">
              {c.completion}
              {changes[i]?.label && <span className={`badge ${changes[i].kind}`}>{changes[i].label}</span>}
              {intentMatches(c, intent).map((m) => (
                <span key={m} className="badge intent">{m}</span>
              ))}
            </span>
            <span className="meta">
              {c.category} · {c.pop_30d} searches · {Math.round(c.ctr * 100)}% engaged
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

export default function App() {
  const [cfg, setCfg] = useState({ qac_debounce_ms: 80 });
  const [scenarios, setScenarios] = useState([]);
  const [scenarioId, setScenarioId] = useState("");
  const [session, setSession] = useState(null);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState(null);
  const [q, setQ] = useState("");
  const [result, setResult] = useState(null);
  const [e2e, setE2e] = useState(null);
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
  }, []);

  useEffect(() => {
    const seq = ++latest.current;
    const pressed = keyAt.current; // null when a new session, not a keystroke, refetched: nothing to time
    keyAt.current = null;
    const t = setTimeout(() => {
      const sent = performance.now();
      const params = new URLSearchParams({ q });
      if (session) params.set("session_id", session.session_id);
      if (session?.user_id) params.set("user_id", session.user_id);
      fetch(`/api/qac?${params}`)
        .then((r) => r.json())
        .then((res) => {
          if (seq !== latest.current) return; // drop responses to older keystrokes
          setResult({ ...res, clock: pressed === null ? null : { pressed, sent, received: performance.now() } });
        });
    }, cfg.qac_debounce_ms);
    return () => clearTimeout(t);
  }, [q, session, cfg.qac_debounce_ms]);

  async function play() {
    setPlaying(true);
    setError(null);
    try {
      const r = await fetch(`/api/scenarios/${scenarioId}/play`, { method: "POST" });
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
                 render: painted - c.received, total: painted - c.pressed });
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [result]);

  const freshness = useFreshness(session);
  const userFeatures = useUserFeatures(session?.user_id);
  const l1 = result?.l1 ?? [];
  const l2 = result?.l2 ?? { status: "not_deployed" };
  const l2Items = l2.items ?? l1;

  return (
    <div className="page">
      {/* A side column, so the panel never moves the search bar */}
      <aside>
        <UnderTheHood session={session} result={result} status={l2.status} budget={cfg.qac_budget_ms ?? 40}
          freshness={freshness} e2e={e2e} />
      </aside>
      <main>
        <h1>Real-time search</h1>

        <section className="scenario">
          <label>
            Scenario
            <select value={scenarioId} onChange={(e) => setScenarioId(e.target.value)}>
              {scenarios.map((s) => (
                <option key={s.scenario_id} value={s.scenario_id}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>
          <button onClick={play} disabled={!scenarioId || playing}>
            {playing ? "Replaying…" : "Play clickstream"}
          </button>
          {session && (
            <p className="session">
              Replayed {session.events} events to Kafka as <code>{session.session_id}</code>
              {session.user_id ? ` (user ${session.user_id})` : " (guest)"}. Try{" "}
              <button className="link" onClick={(e) => { keyAt.current = e.timeStamp; setQ(session.next_query); }}>
                “{session.next_query}”
              </button>
            </p>
          )}
          {error && <p className="error">{error}</p>}
        </section>

        <input
          className="search"
          autoFocus
          placeholder="Start typing…"
          value={q}
          onChange={(e) => { keyAt.current = e.timeStamp; setQ(e.target.value); }}
        />

        <div className="columns">
          <Column title="Static" subtitle="L1: popularity from the prefix table" items={l1} intent={session?.intent} />
          <Column
            title="Session-aware"
            subtitle={
              <>
                L1 + L2: with features from{" "}
                <FeatureBar inputs={cfg.l2_inputs} freshness={freshness} userFeatures={userFeatures}
                  top={l2Items[0]} session={session} />
              </>
            }
            items={l2Items}
            note={L2_STATUS[l2.status] ?? l2.status}
            shown={l2.items ? l1.length : undefined}
            intent={session?.intent}
          />
        </div>
      </main>
      <div />
    </div>
  );
}
