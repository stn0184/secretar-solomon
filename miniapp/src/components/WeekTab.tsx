import { formatWeekRange } from "../lib/format.ts";
import { Empty } from "./Empty.tsx";
import { Header } from "./Header.tsx";

/**
 * Вкладка «Неделя» (`techspec/32-redesign.md` §32.2). Строки дней с часами и
 * сферы полосами строит этап 035 (§32.3); до него — одобренное пустое
 * состояние (`design.md` §2, артборд 08): ни цифры, ни пустой диаграммы.
 * Примеры — нейтральные, без названий сфер владельца.
 */
export function WeekTab({ now, onClose }: { now: Date; onClose: () => void }) {
  return (
    <main className="screen screen--tabs">
      <Header eyebrow={`Неделя · ${formatWeekRange(now)}`} />
      <Empty
        title="За неделю часов нет"
        text="Соломон считает часы по встречам, у которых есть время, и по переписке в личных чатах. Таймеров нет — ничего засекать не нужно."
        examples={["Встреча с Олегом в среду с 11 до 13", "Это по работе: созвон в пятницу с 10 до 11"]}
        hint="Встреча без конца займёт час — поправить можно словом: «до 16»."
        action="Написать Соломону"
        onClose={onClose}
      />
    </main>
  );
}
