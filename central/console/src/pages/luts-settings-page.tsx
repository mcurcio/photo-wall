import { useCallback, useEffect, useState } from "react";

import {
  type AddAnswer,
  type LutView,
  LutsSettings,
  type RemoveAnswer,
  addWords,
  removeWords,
} from "../domain/luts-settings";
import { addLut, listLuts, removeLut } from "../lutsApi.js";
import { Note } from "../patterns/fact-row";
import { ProblemCard } from "../patterns/problem-card";

type Read = { status: "loading" } | { status: "failed" } | { status: "read"; luts: LutView[] };

/**
 * Settings › LUTs (`#/luts`; print look, design "The LUT"; design-language T8, §8 row B5): the
 * LUTs list, Add a paper profile and Remove. Central checks a profile and bakes its LUT once on
 * Add, so every LUT listed works; it refuses to remove one a Display uses. The list is read when
 * the page opens and again after each Add or Remove; a Frame's Picture tab reads it when it opens.
 */
export function LutsSettingsPage() {
  const [read, setRead] = useState<Read>({ status: "loading" });
  const [note, setNote] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const load = useCallback(async () => {
    const answer = await listLuts() as { ok: true; luts: LutView[] } | { ok: false };
    setRead((before) => (answer.ok ? { status: "read", luts: answer.luts }
      : before.status === "read" ? before : { status: "failed" }));
  }, []);
  useEffect(() => { void load(); }, [load]);

  if (read.status === "loading") return <Note>Loading…</Note>;
  if (read.status === "failed") {
    return (
      <ProblemCard scope="central" verdict={{ severity: "unknown", label: "Can't tell", receipt: null }}
        what="Central did not answer with the LUTs list." doing={null}
        action={{ label: "Try again", onAction: () => void load() }} />
    );
  }
  const add = async (file: File) => {
    setAdding(true);
    setNote(null);
    const answer = await addLut(file) as AddAnswer;
    if (answer.ok) await load();
    setAdding(false);
    setNote(addWords(file.name, answer));
  };
  const remove = async (lut: LutView) => {
    setNote(null);
    const answer = await removeLut(lut.profile_sha256) as RemoveAnswer;
    if (answer.ok || answer.error === "unknown_paper_profile" || answer.error === "named_by_a_frame") await load();
    setNote(removeWords(lut, answer));
  };
  return <LutsSettings luts={read.luts} note={note} adding={adding} onAdd={(file) => void add(file)} onRemove={remove} />;
}
