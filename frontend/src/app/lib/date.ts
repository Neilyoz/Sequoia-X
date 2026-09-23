/**
 * 日期工具：全部产出 `YYYY-MM-DD`（约束 03 §9「日期一律 YYYY-MM-DD 展示」），
 * 且全部按**本地时区**计算。
 *
 * 为什么不用 `toISOString().slice(0,10)`：那会先转 UTC，东八区晚上 8 点算出来的
 * "今天"会少一天，看板就会莫名其妙查不到当天信号。所以下面统一手工拼年月日。
 */

/** 把 Date 转成 YYYY-MM-DD（本地时区，月/日补零）。 */
export function toIsoDate(day: Date): string {
  const month = String(day.getMonth() + 1).padStart(2, "0");
  const date = String(day.getDate()).padStart(2, "0");
  return `${day.getFullYear()}-${month}-${date}`;
}

/** 今天的 YYYY-MM-DD。 */
export function today(): string {
  return toIsoDate(new Date());
}

/** 以今天为终点往前推 days-1 天的区间（含今天，共 days 天）。 */
export function lastNDays(days: number): DateRange {
  const end = new Date();
  const start = new Date();
  start.setDate(start.getDate() - (days - 1));
  return { start: toIsoDate(start), end: toIsoDate(end) };
}

/** 本月：1 号到今天（自然月，不看是否交易日）。 */
export function thisMonth(): DateRange {
  const now = new Date();
  return { start: toIsoDate(new Date(now.getFullYear(), now.getMonth(), 1)), end: toIsoDate(now) };
}

/** 区间端点：start/end 都是闭区间，与后端 `trade_date >= ? AND trade_date <= ?` 对齐。 */
export type DateRange = {
  start: string;
  end: string;
};
