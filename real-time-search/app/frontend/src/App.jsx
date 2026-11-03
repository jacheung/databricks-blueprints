import { useEffect, useRef, useState } from "react";

const L2_STATUS = {
  not_deployed: "L2 not deployed: showing the L1 order",
  ok: "Reranked with live session features",
  timeout: "L2 timed out: showing the L1 order",
};

function Column({ title, subtitle, items, note }) {
  return (
    <section className="column">
      <header>
        <h2>{title}</h2>
        <p className="subtitle">{subtitle}</p>
      </header>
      {note && <p className="note">{note}</p>}
      <ol>
        {items.map((c) => (
          <li key={c.completion}>
            <span className="completion">{c.completion}</span>
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
  const l2 = result?.l2 ?? { status: "not_deployed", items: [] };

  return (
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
            Replayed {session.events} events as <code>{session.session_id}</code>
            {session.user_id ? ` (user ${session.user_id})` : " (guest)"}. This shopper's next search was{" "}
            <button className="link" onClick={() => setQ(session.next_query)}>
              “{session.next_query}”
            </button>
            : try typing its first letters.
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
        </p>
      )}

      <div className="columns">
        <Column title="Static" subtitle="L1: popularity from the prefix table" items={l1} />
        <Column
          title="Session-aware"
          subtitle="L1 + L2: reranked with clickstream features"
          items={l2.items}
          note={L2_STATUS[l2.status] ?? l2.status}
        />
      </div>
    </main>
  );
}
