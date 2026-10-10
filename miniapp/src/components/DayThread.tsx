import type { CSSProperties } from "react";

import { formatTime, hoursWords } from "../lib/format.ts";
import { type ThreadEntry, type ThreadEvent, capsuleHeight, eventMeta } from "../lib/hours.ts";
import { type Sphere, sphereColor, sphereOf } from "../lib/spheres.ts";

/** Значок капсулы — из чата (`techspec/29-icons.md`): 📅 — с часом, 💬 — переписка. */
const ICON: Record<ThreadEvent["kind"], string> = { meeting: "📅", point: "📅", chat: "💬" };

function capClass(event: ThreadEvent): string {
  return [
    "cap",
    event.kind === "chat" ? "cap--chat" : "",
    event.kind === "point" ? "cap--point" : "",
    event.state === "future" ? "cap--future" : "",
  ]
    .filter(Boolean)
    .join(" ");
}

function Event({ event, spheres }: { event: ThreadEvent; spheres: readonly Sphere[] }) {
  const height = capsuleHeight(event.minutes);
  return (
    <div
      className={event.state === "past" ? "ti" : `ti ti--${event.state}`}
      style={{ "--c": sphereColor(sphereOf(event.sphereId, spheres)), minHeight: `${height + 12}px` } as CSSProperties}
    >
      <span className="ti__time">{formatTime(event.start)}</span>
      <span className="ti__rail" aria-hidden="true">
        <span className={capClass(event)} style={{ height: `${height}px` }}>
          {ICON[event.kind]}
        </span>
      </span>
      <span className="ti__b">
        <span className="ti__t">{event.title}</span>
        <span className="ti__m">{eventMeta(event, spheres)}</span>
      </span>
      {event.state === "done" ? <span className="done" role="img" aria-label="Сделано" /> : <span />}
    </div>
  );
}

/**
 * Нить дня (артборд 01): капсулы событий по времени — встреча заливкой
 * цвета сферы, высотой по длительности; дело с часом без длительности —
 * маленькой точкой; переписка — бледной капсулой с кантом; будущее — контуром.
 * Между событиями — «Свободно 40 мин», линия «сейчас» — перед будущим.
 * ✓ у сделанного — только состояние: нить не нажимается, «Сделано» — в
 * карточке дела.
 */
export function DayThread({ entries, spheres }: { entries: readonly ThreadEntry[]; spheres: readonly Sphere[] }) {
  return (
    <div className="thread">
      {entries.map((entry) => {
        if (entry.type === "gap") {
          return (
            <div key={entry.key} className="gap">
              <span />
              <span className="gap__rail" aria-hidden="true" />
              <p>Свободно {hoursWords(entry.minutes)}</p>
            </div>
          );
        }
        if (entry.type === "now") {
          return (
            <div key="now" className="now">
              <span>{formatTime(entry.at)}</span>
              <i aria-hidden="true" />
            </div>
          );
        }
        return <Event key={entry.event.key} event={entry.event} spheres={spheres} />;
      })}
    </div>
  );
}
