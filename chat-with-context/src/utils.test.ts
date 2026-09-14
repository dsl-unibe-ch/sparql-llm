import {describe, expect, test} from "vitest";

import {groupByRecency} from "./utils";

describe("sidebar grouping", () => {
  const now = new Date(2026, 8, 14, 18, 0);
  const at = (days: number, hour = 9) => ({updated_at: new Date(2026, 8, 14 - days, hour).toISOString()});

  test("chats fall into today, the previous seven days, and older", () => {
    const groups = groupByRecency([at(0), at(1), at(7), at(8)], now);
    expect(groups.map(g => [g.label, g.items.length])).toEqual([
      ["Today", 1],
      ["Previous 7 days", 2],
      ["Older", 1],
    ]);
  });

  test("empty groups are left out", () => {
    expect(groupByRecency([at(30)], now).map(g => g.label)).toEqual(["Older"]);
    expect(groupByRecency([], now)).toEqual([]);
  });

  test("a chat from just after midnight counts as today", () => {
    expect(groupByRecency([at(0, 0)], now)[0].label).toBe("Today");
  });
});
