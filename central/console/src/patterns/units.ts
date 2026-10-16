/*
 * The units the console writes a measured value in, and the one way each is written: a number
 * with its unit after a space ("1,000 lx", "2700 K", "12.5 mm", "45°", "60 %"). Pure.
 */

export type Unit = "lx" | "K" | "mm" | "°" | "%";

/** Below this many lux a reading keeps one decimal ("2.5 lx"); from it, whole lux. */
const LUX_DECIMALS_BELOW = 10;
/** A percent's most decimals: a panel's black is a few thousandths of a percent of its white. */
const PERCENT_DECIMALS = 3;
/** A run of three digits, from the right: where a thousands separator goes. */
const THOUSANDS = /\B(?=(\d{3})+$)/g;

/** Whole numbers with their thousands separated by commas ("200,000"). */
function grouped(value: number): string {
  const whole = Math.round(value);
  const sign = whole < 0 ? "-" : "";
  return sign + String(Math.abs(whole)).replace(THOUSANDS, ",");
}

/** A number in its shortest form up to `decimals` places ("12.5", "12", never "12.50"). */
function shortest(value: number, decimals: number): string {
  return String(Number(value.toFixed(decimals)));
}

/**
 * `value` in `unit`, as the console writes it: lux whole with thousands separated ("1,000 lx"),
 * one decimal below 10 lx ("2.5 lx"); kelvin whole ("2700 K"); millimetres to one decimal at most
 * ("12.5 mm"); percent to three decimals at most, for a share as small as a panel's black
 * ("60 %", "0.03 %"); degrees to one decimal at most, with no space ("45°").
 */
export function unitWords(value: number, unit: Unit): string {
  switch (unit) {
    case "lx":
      return `${Math.abs(value) < LUX_DECIMALS_BELOW ? shortest(value, 1) : grouped(value)} lx`;
    case "K":
      return `${Math.round(value)} K`;
    case "°":
      return `${shortest(value, 1)}°`;
    case "%":
      return `${shortest(value, PERCENT_DECIMALS)} %`;
    default:
      return `${shortest(value, 1)} ${unit}`;
  }
}
