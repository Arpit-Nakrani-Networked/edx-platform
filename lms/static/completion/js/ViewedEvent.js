/** Ensure that a function is only run once every `wait` milliseconds */
// eslint-disable-next-line max-classes-per-file
function throttle(fn, wait) {
    let time = 0;
    function delay() {
    // Do not call the function until at least `wait` seconds after the
    // last time the function was called.
        const now = Date.now();
        if (time + wait < now) {
            time = now;
            fn();
        }
    }
    return delay;
}

export class ElementViewing {
    /**
   * A wrapper for an HTMLElement that tracks whether the element has been
   * viewed or not.
   */
    constructor(el, viewedAfterMs, callback) {
        this.el = el;
        this.viewedAfterMs = viewedAfterMs;
        this.callback = callback;

        this.topSeen = false;
        this.bottomSeen = false;
        this.seenForMs = 0;
        this.becameVisibleAt = undefined;
        this.hasBeenViewed = false;
    }

    getBoundingRect() {
        return this.el.getBoundingClientRect();
    }

    /** This element has become visible on screen.
   *
   * (may be called even when already on screen though)
   */
    handleVisible() {
        if (!this.becameVisibleAt) {
            this.becameVisibleAt = Date.now();
            // Pure time-based completion - only time matters
            if (this.viewedAfterMs === 0) {
                // Immediate completion for 0 delay
                this.checkIfViewed();
            } else {
                // Set timeout for required time
                setTimeout(
                    () => {
                        this.checkIfViewed();
                    },
                    this.viewedAfterMs - this.seenForMs,
                );
            }
        }
    }

    handleNotVisible() {
        if (this.becameVisibleAt) {
            this.seenForMs = Date.now() - this.becameVisibleAt;
        }
        this.becameVisibleAt = undefined;
    }

    markTopSeen() {
        // Pure time-based completion - visibility tracking not needed for completion
        this.topSeen = true;
        // Remove checkIfViewed() call - only time triggers completion now
    }

    markBottomSeen() {
        // Pure time-based completion - visibility tracking not needed for completion
        this.bottomSeen = true;
        // Remove checkIfViewed() call - only time triggers completion now
    }

    getTotalTimeSeen() {
        if (this.becameVisibleAt) {
            return this.seenForMs + (Date.now() - this.becameVisibleAt);
        }
        return this.seenForMs;
    }

    areViewedCriteriaMet() {
        // Pure time-based completion - only time matters, not visibility
        return (this.getTotalTimeSeen() >= this.viewedAfterMs || this.viewedAfterMs === 0);
    }

    checkIfViewed() {
    // User can provide a "now" value for testing purposes.
        if (this.hasBeenViewed) {
            return;
        }
        if (this.areViewedCriteriaMet()) {
            this.hasBeenViewed = true;
            // Report to the tracker that we have been viewed
            this.callback(this.el, { elementHasBeenViewed: this.hasBeenViewed });
        }
    }
}

export class ViewedEventTracker {
    /**
   * When the top or bottom of an element is first viewed, and the entire
   * element is viewed for a specified amount of time, the callback is called,
   * passing the element that was viewed, and an event object having the
   * following field:
   *
   * *   hasBeenViewed (bool): true if all the conditions for being
   *     considered "viewed" have been met.
   */
    constructor() {
        this.elementViewings = new Set();
        this.handlers = [];
        this.registerDomHandlers();
    }

    /** Add an element to track.  */
    addElement(element, viewedAfterMs) {
        const elementViewing = new ElementViewing(
            element,
            viewedAfterMs,
            (el, event) => this.callHandlers(el, event),
        );
        this.elementViewings.add(elementViewing);

        // If viewedAfterMs is 0, mark as viewed immediately regardless of visibility
        // Use setTimeout to ensure handlers have been registered first
        if (viewedAfterMs === 0) {
            elementViewing.hasBeenViewed = true;
            setTimeout(() => {
                this.callHandlers(element, { elementHasBeenViewed: true });
            }, 0);
        }

        this.updateVisible();
    }

    /** Register a new handler to be called when an element has been viewed.  */
    addHandler(handler) {
        this.handlers.push(handler);
    }

    /** Mark which elements are currently visible.
   *
   *  Also marks when an elements top or bottom has been seen.
   * */
    updateVisible() {
        this.elementViewings.forEach((elv) => {
            if (elv.hasBeenViewed) {
                return;
            }

            const now = Date.now(); // Use the same "now" for all calculations
            const rect = elv.getBoundingRect();
            let visible = false;

            if (rect.top > 0 && rect.top < window.innerHeight) {
                elv.markTopSeen(now);
                visible = true;
            }
            if (rect.bottom > 0 && rect.bottom < window.innerHeight) {
                elv.markBottomSeen(now);
                visible = true;
            }
            if (rect.top < 0 && rect.bottom > window.innerHeight) {
                visible = true;
            }

            if (visible) {
                elv.handleVisible(now);
            } else {
                elv.handleNotVisible(now);
            }
        });
    }

    registerDomHandlers() {
        window.onscroll = throttle(() => this.updateVisible(), 100);
        window.onresize = throttle(() => this.updateVisible(), 100);
        this.updateVisible();
    }

    /** Call the handlers for all newly-viewed elements and pause tracking
   *  for recently disappeared elements.
   */
    callHandlers(el, event) {
        this.handlers.forEach((handler) => {
            handler(el, event);
        });
    }
}