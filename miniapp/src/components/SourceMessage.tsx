import { formatMoment } from "../lib/format.ts";
import { type SourceMessage as Source, messageCaption, messageLines } from "../lib/tasks.ts";

/**
 * Исходное сообщение целиком — видно, что именно помощник читал. В карточке
 * дела — цитатой на второй поверхности (артборд 05): над ней «Голосовое ·
 * 0:12 · четверг, 8 октября, 16:26», у текста — «Текст · …», чтобы было ясно,
 * откуда ошибки в словах (`techspec/09-voice.md` §9.4). У снимка — «Фото», в
 * цитате подпись и «Со снимка: …» (`techspec/14-photo.md` §14.4); сам снимок
 * не показывается — он не хранится. Сообщения не осталось — короткая подпись.
 *
 * `compact` — внутри раскрытой записи памяти: без своей поверхности, только
 * подпись с датой и цитата; `tone="guess"` красит кромку цветом
 * предположения — из этого сообщения помощник вывел, а не прочитал.
 */
export function SourceMessage({
  message,
  now,
  compact = false,
  tone,
}: {
  message: Source | null;
  now: Date;
  compact?: boolean;
  tone?: "guess";
}) {
  if (!message) {
    return <p className="note">Исходного сообщения нет.</p>;
  }
  const caption = messageCaption(message);
  const quote = messageLines(message).map((line, index) => (
    <p key={index} className="source__text">
      {line}
    </p>
  ));

  if (compact) {
    const box = tone === "guess" ? "source__box source__box--guess" : "source__box";
    return (
      <div className="source source--inline">
        <div className="source__top">
          <span>{caption ? `Исходное сообщение · ${caption}` : "Исходное сообщение"}</span>
          <span>{formatMoment(message.receivedAt, now)}</span>
        </div>
        <blockquote className={box}>{quote}</blockquote>
      </div>
    );
  }

  return (
    <section className="src" aria-label="Исходное сообщение">
      <span className="src__h">
        {caption ?? "Текст"} · {formatMoment(message.receivedAt, now)}
      </span>
      <blockquote className="src__q">{quote}</blockquote>
    </section>
  );
}
