export class Mutex {
  constructor() {
    this.tail = Promise.resolve();
    this.pending = 0;
  }

  get size() {
    return this.pending;
  }

  async run(fn) {
    this.pending += 1;
    const previous = this.tail;
    let release;
    this.tail = new Promise(resolve => {
      release = resolve;
    });

    await previous;
    try {
      return await fn();
    } finally {
      this.pending -= 1;
      release();
    }
  }
}
