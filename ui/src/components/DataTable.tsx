import { useEffect, useMemo, useState, type ReactNode } from "react";

export type SortDir = "asc" | "desc";

export interface Column<T> {
  key: string;
  header: string;
  align?: "left" | "right";
  cell: (row: T) => ReactNode;
  footer?: ReactNode;
  sortValue?: (row: T) => string | number | null;
  /** When true, column cannot be hidden via the picker. */
  locked?: boolean;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T, index: number) => string;
  empty?: ReactNode;
  /** Persist column visibility under this localStorage key. */
  storageKey?: string;
  defaultSortKey?: string;
  defaultSortDir?: SortDir;
}

interface ColPrefs {
  hidden: string[];
}

function loadPrefs(storageKey: string | undefined): ColPrefs {
  if (!storageKey) return { hidden: [] };
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return { hidden: [] };
    const parsed = JSON.parse(raw) as { hidden?: unknown };
    if (!Array.isArray(parsed.hidden)) return { hidden: [] };
    return {
      hidden: parsed.hidden.filter((k): k is string => typeof k === "string"),
    };
  } catch {
    return { hidden: [] };
  }
}

function savePrefs(storageKey: string | undefined, prefs: ColPrefs): void {
  if (!storageKey) return;
  try {
    localStorage.setItem(storageKey, JSON.stringify(prefs));
  } catch {
    /* ignore quota / private mode */
  }
}

function compareValues(
  a: string | number | null,
  b: string | number | null,
  dir: SortDir,
): number {
  const mul = dir === "asc" ? 1 : -1;
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  if (typeof a === "number" && typeof b === "number") {
    if (Number.isNaN(a) && Number.isNaN(b)) return 0;
    if (Number.isNaN(a)) return 1;
    if (Number.isNaN(b)) return -1;
    return (a - b) * mul;
  }
  return String(a).localeCompare(String(b), undefined, { numeric: true }) * mul;
}

export default function DataTable<T>({
  columns,
  rows,
  rowKey,
  empty,
  storageKey,
  defaultSortKey,
  defaultSortDir = "desc",
}: DataTableProps<T>) {
  const [sortKey, setSortKey] = useState<string | null>(defaultSortKey ?? null);
  const [sortDir, setSortDir] = useState<SortDir | null>(
    defaultSortKey ? defaultSortDir : null,
  );
  const [hidden, setHidden] = useState<string[]>(() => loadPrefs(storageKey).hidden);
  const [pickerOpen, setPickerOpen] = useState(false);

  useEffect(() => {
    setHidden(loadPrefs(storageKey).hidden);
  }, [storageKey]);

  const lockedKeys = useMemo(
    () => new Set(columns.filter((c) => c.locked).map((c) => c.key)),
    [columns],
  );

  const visibleColumns = useMemo(
    () => columns.filter((c) => c.locked || !hidden.includes(c.key)),
    [columns, hidden],
  );

  const sortedRows = useMemo(() => {
    if (!sortKey || !sortDir) return rows;
    const col = columns.find((c) => c.key === sortKey);
    if (!col?.sortValue) return rows;
    const get = col.sortValue;
    return [...rows].sort((ra, rb) =>
      compareValues(get(ra), get(rb), sortDir),
    );
  }, [rows, columns, sortKey, sortDir]);

  const cycleSort = (key: string) => {
    const col = columns.find((c) => c.key === key);
    if (!col?.sortValue) return;
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir("asc");
      return;
    }
    if (sortDir === "asc") {
      setSortDir("desc");
      return;
    }
    setSortKey(null);
    setSortDir(null);
  };

  const toggleColumn = (key: string) => {
    if (lockedKeys.has(key)) return;
    setHidden((prev) => {
      const next = prev.includes(key)
        ? prev.filter((k) => k !== key)
        : [...prev, key];
      savePrefs(storageKey, { hidden: next });
      return next;
    });
  };

  if (rows.length === 0 && empty) {
    return <>{empty}</>;
  }

  const hasFooter = visibleColumns.some((c) => c.footer !== undefined);

  return (
    <div className="table-panel">
      {storageKey ? (
        <div className="table-toolbar">
          <div className="col-picker">
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              aria-expanded={pickerOpen}
              onClick={() => setPickerOpen((v) => !v)}
            >
              Columns
            </button>
            {pickerOpen ? (
              <div className="col-picker-menu" role="menu">
                {columns.map((col) => {
                  const locked = lockedKeys.has(col.key);
                  const checked = locked || !hidden.includes(col.key);
                  return (
                    <label
                      key={col.key}
                      className={
                        locked ? "col-picker-item locked" : "col-picker-item"
                      }
                    >
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={locked}
                        onChange={() => toggleColumn(col.key)}
                      />
                      {col.header}
                    </label>
                  );
                })}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
      <div className="table-wrap">
        <table className="data-table">
          <thead>
            <tr>
              {visibleColumns.map((col) => {
                const sortable = Boolean(col.sortValue);
                const active = sortKey === col.key && sortDir !== null;
                return (
                  <th
                    key={col.key}
                    className={[
                      col.align === "right" ? "num" : undefined,
                      sortable ? "sortable" : undefined,
                      active ? `sorted sorted-${sortDir}` : undefined,
                    ]
                      .filter(Boolean)
                      .join(" ") || undefined}
                    onClick={sortable ? () => cycleSort(col.key) : undefined}
                    aria-sort={
                      active
                        ? sortDir === "asc"
                          ? "ascending"
                          : "descending"
                        : sortable
                          ? "none"
                          : undefined
                    }
                  >
                    <span className="th-label">{col.header}</span>
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {sortedRows.map((row, i) => (
              <tr key={rowKey(row, i)}>
                {visibleColumns.map((col) => (
                  <td
                    key={col.key}
                    className={col.align === "right" ? "num" : undefined}
                  >
                    {col.cell(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
          {hasFooter ? (
            <tfoot>
              <tr>
                {visibleColumns.map((col) => (
                  <td
                    key={col.key}
                    className={col.align === "right" ? "num" : undefined}
                  >
                    {col.footer ?? null}
                  </td>
                ))}
              </tr>
            </tfoot>
          ) : null}
        </table>
      </div>
    </div>
  );
}
