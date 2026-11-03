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

function Timeline({ session }) {
  const { intent, timeline } = session;
  return (
    <section className="timeline">
      <p className="intent">
        Planted intent: <strong>{[intent.category, intent.brand, intent.price_target && `~$${Math.round(intent.price_target)}`]
          .filter(Boolean).join(" · ")}</strong>
        . Expect the session-aware column to lift completions that match it.
      </p>
      <ol>
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
    </section>
  );
}

function Column({ title, subtitle, items, note, shown, intent }) {
  const changes = items.map((c, i) => (shown === undefined ? null : rankChange(c, i + 1, shown)));
  const diffed = changes.some((ch) => ch);
  return (
    <section className="column">
      <header>
        <h2>{title}</h2>
        <p className="subtitle">{subtitle}</p>
      </header>
      {note && <p className="note">{note}</p>}
      {diffed && <p className="summary">{diffSummary(changes)}</p>}
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
  const latest = useRef(0);

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
    const t = setTimeout(() => {
      const params = new URLSearchParams({ q });
      if (session) params.set("session_id", session.session_id);
      if (session?.user_id) params.set("user_id", session.user_id);
      fetch(`/api/qac?${params}`)
        .then((r) => r.json())
        .then((res) => seq === latest.current && setResult(res)); // drop responses to older keystrokes
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

  const l1 = result?.l1 ?? [];
  const l2 = result?.l2 ?? { status: "not_deployed" };

  return (
    <div className="page">
      {/* A side column, so the timeline never moves the search bar */}
      <aside>{session && <Timeline session={session} />}</aside>
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
              <button className="link" onClick={() => setQ(session.next_query)}>
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
          onChange={(e) => setQ(e.target.value)}
        />
        {result && (
          <p className="timing">
            prefix “{result.prefix}” · L1 lookup {result.timings_ms.l1} ms
            {result.timings_ms.l2 !== undefined && ` · L2 ${result.timings_ms.l2} ms`}
          </p>
        )}

        <div className="columns">
          <Column title="Static" subtitle="L1: popularity from the prefix table" items={l1} intent={session?.intent} />
          <Column
            title="Session-aware"
            subtitle="L1 + L2: reranked with clickstream features"
            items={l2.items ?? l1}
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
