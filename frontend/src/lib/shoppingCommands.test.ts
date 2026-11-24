import { expect, it } from "vitest";
import { navigationText, selectText } from "./shoppingCommands";

it("uses the existing navigation parser's command phrases", () => {
  expect(navigationText("previous")).toBe("上一个");
  expect(navigationText("next")).toBe("换一个");
  expect(navigationText("compare")).toBe("对比");
});

it("uses the stable displayed position in a selection command", () => {
  expect(selectText(1)).toBe("选第1个");
  expect(selectText(12)).toBe("选第12个");
});
