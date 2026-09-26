"use client";

import { useEffect, useRef } from "react";
import type { IChartApi, Time } from "lightweight-charts";
import type { OhlcvItem } from "../lib/types";

const UP_COLOR = "#dc2626";
const DOWN_COLOR = "#16a34a";

type Props = {
  items: OhlcvItem[];
  height?: number;
};

export default function KlineChart({ items, height = 360 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container || items.length === 0) {
      return;
    }

    let chart: IChartApi | null = null;
    let cancelled = false;

    async function renderChart(): Promise<void> {
      const { CandlestickSeries, ColorType, HistogramSeries, createChart } =
        await import("lightweight-charts");
      if (cancelled) {
        return;
      }

      chart = createChart(container!, {
        autoSize: true,
        height,
        layout: {
          background: { type: ColorType.Solid, color: "#ffffff" },
          textColor: "#52525b",
          attributionLogo: true,
        },
        grid: {
          vertLines: { color: "#f4f4f5" },
          horzLines: { color: "#f4f4f5" },
        },
        rightPriceScale: { borderColor: "#e4e4e7" },
        timeScale: { borderColor: "#e4e4e7" },
      });

      const candleSeries = chart.addSeries(CandlestickSeries, {
        upColor: UP_COLOR,
        downColor: DOWN_COLOR,
        borderVisible: false,
        wickUpColor: UP_COLOR,
        wickDownColor: DOWN_COLOR,
      });
      const volumeSeries = chart.addSeries(
        HistogramSeries,
        {
          priceFormat: { type: "volume" },
          priceLineVisible: false,
          lastValueVisible: false,
        },
        1,
      );

      candleSeries.setData(
        items.map((item) => ({
          time: item.date as Time,
          open: item.open,
          high: item.high,
          low: item.low,
          close: item.close,
          color: item.close >= item.open ? UP_COLOR : DOWN_COLOR,
        })),
      );
      volumeSeries.setData(
        items.map((item) => ({
          time: item.date as Time,
          value: item.volume,
          color: item.close >= item.open ? UP_COLOR : DOWN_COLOR,
        })),
      );

      const panes = chart.panes();
      if (panes.length > 1) {
        panes[1].setHeight(Math.max(72, height * 0.24));
      }
      chart.timeScale().fitContent();
    }

    void renderChart();
    return () => {
      cancelled = true;
      chart?.remove();
    };
  }, [height, items]);

  return (
    <div>
      <div ref={containerRef} style={{ height }} className="w-full" />
      <p className="mt-2 text-right text-[10px] text-zinc-400">
        TradingView Lightweight Charts™ · Copyright © 2025 TradingView, Inc. ·{" "}
        <a
          href="https://www.tradingview.com/"
          target="_blank"
          rel="noreferrer"
          className="underline underline-offset-2"
        >
          TradingView
        </a>
      </p>
    </div>
  );
}
