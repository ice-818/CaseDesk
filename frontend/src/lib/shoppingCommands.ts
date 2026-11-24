const navigation = { previous: "上一个", next: "换一个", compare: "对比" } as const;

export function navigationText(kind: keyof typeof navigation) {
  return navigation[kind];
}

export function selectText(position: number): string {
  return `选第${position}个`;
}
