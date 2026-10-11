"use strict";

// UI range arithmetic only. The official SDK still generates every order.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.PriceRange = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  const floor = (price, tick) => price / tick * tick;
  const ceil = (price, tick) => (price + tick - 1n) / tick * tick;
  const clamp = (value, minimum, maximum) => value < minimum ? minimum : value > maximum ? maximum : value;

  class Selection {
    constructor(referenceRaw, tickRaw, minimumRaw, maximumRaw) {
      this.reference = BigInt(referenceRaw);
      this.tick = BigInt(tickRaw);
      if (this.reference <= 0n || this.tick <= 0n) throw new Error("Price and tick must be positive.");
      this.setBounds(minimumRaw, maximumRaw);
      const width = this.maximum - this.minimum;
      const margin = width * 3n / 4n > this.tick * 10n ? width * 3n / 4n : this.tick * 10n;
      this.domainMinimum = floor(this.reference > margin ? this.reference - margin : this.tick, this.tick);
      this.domainMaximum = ceil(this.reference + margin, this.tick);
      this.extendDomain();
    }

    setBounds(minimumRaw, maximumRaw) {
      const minimum = floor(BigInt(minimumRaw), this.tick);
      const maximum = ceil(BigInt(maximumRaw), this.tick);
      if (minimum < this.tick || maximum <= minimum) throw new Error("Minimum price must be positive and below maximum price.");
      this.minimum = minimum;
      this.maximum = maximum;
      if (this.domainMinimum !== undefined) this.extendDomain();
      return this.bounds();
    }

    extendDomain() {
      const padding = (this.maximum - this.minimum) / 5n + this.tick;
      if (this.minimum < this.domainMinimum) this.domainMinimum = floor(this.minimum > padding ? this.minimum - padding : this.tick, this.tick);
      if (this.maximum > this.domainMaximum) this.domainMaximum = ceil(this.maximum + padding, this.tick);
    }

    get center() { return (this.minimum + this.maximum) / 2n; }
    bounds() { return { minimumPriceRaw: this.minimum, maximumPriceRaw: this.maximum }; }
    fraction(price) { return Number(BigInt(price) - this.domainMinimum) / Number(this.domainMaximum - this.domainMinimum); }
    priceAt(fraction) {
      const span = this.domainMaximum - this.domainMinimum;
      if (span > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error("This price span exceeds the draggable control limit. Enter prices directly.");
      const raw = this.domainMinimum + BigInt(Math.round(Math.min(1, Math.max(0, fraction)) * Number(span)));
      return floor(raw + this.tick / 2n, this.tick);
    }

    moveEdge(side, raw) {
      if (side === "minimum") this.minimum = clamp(floor(BigInt(raw), this.tick), this.tick, this.maximum - this.tick);
      else if (side === "maximum") this.maximum = clamp(ceil(BigInt(raw), this.tick), this.minimum + this.tick, this.domainMaximum);
      else throw new Error("Unknown range handle.");
      return this.bounds();
    }

    moveCenter(raw) {
      // Translate in whole ticks. Keep the existing width and grid alignment.
      const desired = BigInt(raw);
      let shift = (desired - this.center) / this.tick * this.tick;
      shift = clamp(shift, this.tick - this.minimum, this.domainMaximum - this.maximum);
      this.minimum += shift;
      this.maximum += shift;
      return this.bounds();
    }

    reset() {
      const span = this.maximum - this.minimum;
      const minimum = this.reference - span / 2n;
      if (minimum < this.tick) throw new Error("This range is too wide to center at the starting price.");
      return this.setBounds(minimum, this.reference + (span - span / 2n));
    }
  }
  // Signed percentages are UI offsets from the historical price, not a new
  // historical mark. Use integer arithmetic before outward tick rounding.
  function offsetBounds(referenceRaw, lowerOffset, upperOffset, tickRaw, decimalToRaw) {
    const reference = BigInt(referenceRaw);
    const tick = BigInt(tickRaw);
    const scale = 100000000n;
    const parse = (text) => {
      const value = String(text).trim();
      const negative = value.startsWith("-");
      const magnitude = decimalToRaw(value.replace(/^[+-]/, ""), 6);
      return negative ? -magnitude : magnitude;
    };
    const lower = parse(lowerOffset);
    const upper = parse(upperOffset);
    if (lower <= -scale || upper <= lower) throw new Error("Price offsets must be above −100%, with min below max.");
    return {
      minimumPriceRaw: floor(reference * (scale + lower) / scale, tick) || tick,
      maximumPriceRaw: (reference * (scale + upper) + scale * tick - 1n) / (scale * tick) * tick,
    };
  }
  return { Selection, offsetBounds };
});
