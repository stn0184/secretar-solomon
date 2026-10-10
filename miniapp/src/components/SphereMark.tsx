import { type Sphere, monogram } from "../lib/spheres.ts";

/**
 * Отметка сферы в строке дела (`design.md` §3): одна разметка, вид — у темы.
 * В «Банк-люксе» — круг цвета сферы с монограммой, как логотип; в «Тихой
 * роскоши» — тонкая полоска того же цвета, буквы скрыты. Цвет приходит
 * переменной `--c` от строки. Без сферы — серая отметка с точкой.
 */
export function SphereMark({ sphere }: { sphere: Sphere | null }) {
  return (
    <span className="mark" aria-hidden="true">
      {sphere ? monogram(sphere.name) : "·"}
    </span>
  );
}
