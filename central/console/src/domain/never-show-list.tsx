import type { NeverShowEntry } from "../generated/contract";
import { CONSTANTS } from "../generated/contract";
import { EntityList } from "../patterns/entity-list";
import { Note } from "../patterns/fact-row";
import { PhotoLine } from "../patterns/photo-line";
import { Button } from "../ui/button";
import { Stack } from "../ui/stack";
import { listedWords, NEVER_SHOW_EMPTY, photoWords } from "./everyday-words";

type Row = NeverShowEntry & { key: string; severity: null };

export interface NeverShowListProps {
  photos: readonly NeverShowEntry[];
  /** Central's time of the read: each "when" is said from it. */
  now: number | null;
  /** A Show again is on its way to Central: the buttons wait. */
  busy: boolean;
  onShowAgain: (photo: NeverShowEntry) => void;
  /** Where a photo's thumbnail is read; Central's own route unless a story draws its own. */
  thumbnail?: (assetId: string) => string;
}

const centralThumbnail = (assetId: string) => `${CONSTANTS.THUMBNAIL_PATH}${encodeURIComponent(assetId)}`;

/**
 * Scenes › Never show list (decision 0036, F3; setting #71): every photo a person said never to
 * show again, newest first, each with who listed it and when, and **Show again**. The list is the
 * wall's own: nothing is written to Immich. A photo's picture is Central's thumbnail, drawn whole
 * (never cropped), in a fixed box. The page reads and writes; this is what a story draws.
 */
export function NeverShowList({ photos, now, busy, onShowAgain, thumbnail = centralThumbnail }: NeverShowListProps) {
  const rows: Row[] = [...photos].sort((a, b) => b.listed_at - a.listed_at)
    .map((photo) => ({ ...photo, key: photo.asset_id, severity: null }));
  return (
    <Stack>
      <Note>
        Photos here never show on the wall again, on any Frame. Nothing is changed in Immich.
      </Note>
      <EntityList<Row>
        label="Never show list"
        columns={[
          {
            id: "photo", header: "Photo", rowHeader: true,
            cell: (row) => <PhotoLine src={thumbnail(row.asset_id)}>{photoWords(row.kind)}</PhotoLine>,
          },
          { id: "listed", header: "Listed", cell: (row) => listedWords(row, now) },
          {
            id: "actions", header: "Actions",
            cell: (row) => (
              <Button variant="ghost" disabled={busy} aria-label={`Show again: ${photoWords(row.kind)} listed ${listedWords(row, now)}`}
                onClick={() => onShowAgain(row)}>
                Show again
              </Button>
            ),
          },
        ]}
        groups={[{ title: "Never shown", rows }]}
        empty={NEVER_SHOW_EMPTY}
      />
    </Stack>
  );
}
