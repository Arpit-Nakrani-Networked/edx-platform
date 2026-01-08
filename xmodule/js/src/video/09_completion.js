(function(define) {
    'use strict';

    /**
     * Completion  handler
     * @exports video/09_completion.js
     * @constructor
     * @param {Object} state The object containing the state of the video
     * @return {jquery Promise}
     */
    define('video/09_completion.js', [], function() {
        var VideoCompletionHandler = function(state) {
            if (!(this instanceof VideoCompletionHandler)) {
                return new VideoCompletionHandler(state);
            }
            this.state = state;
            this.state.completionHandler = this;
            this.initialize();
            return $.Deferred().resolve().promise();
        };

        VideoCompletionHandler.prototype = {

            /** Tears down the VideoCompletionHandler.
             *
             *  * Removes backreferences from this.state to this.
             *  * Turns off signal handlers.
             */
            destroy: function() {
                // Clear any pending completion timeout
                if (this.completionTimeoutId) {
                    clearTimeout(this.completionTimeoutId);
                    this.completionTimeoutId = null;
                }
                // Stop tracking viewing time
                if (this.isCurrentlyViewing) {
                    this.handlePause();
                }
                this.el.remove();
                this.el.off('play');
                this.el.off('pause');
                this.el.off('timeupdate.completion');
                this.el.off('ended.completion');
                delete this.state.completionHandler;
            },

            /** Initializes the VideoCompletionHandler.
             *
             *  This sets all the instance variables needed to perform
             *  completion calculations.
             */
            initialize: function() {
                // Attributes with "Time" in the name refer to the number of seconds since
                // the beginning of the video, except for lastSentTime, which refers to a
                // timestamp in seconds since the Unix epoch.
                this.lastSentTime = undefined;
                this.isComplete = false;
                // Minimum time user must spend watching before completion - from subsection settings
                this.minimumViewingTimeMs = this.state.config.minimumViewingTimeMs || 0;
                this.completionTimeoutId = null;
                // Track viewing time
                this.totalViewingTimeMs = 0;
                this.viewingStartedAt = null;
                this.isCurrentlyViewing = false;
                // If prevent_skip_video is enabled, require 100% completion
                if (this.state.config.preventSkipVideo) {
                    this.completionPercentage = 1.0;
                } else {
                    this.completionPercentage = this.state.config.completionPercentage;
                }
                this.startTime = this.state.config.startTime;
                this.endTime = this.state.config.endTime;
                this.isEnabled = this.state.config.completionEnabled;
                if (this.endTime) {
                    this.completeAfterTime = this.calculateCompleteAfterTime(this.startTime, this.endTime);
                }
                if (this.isEnabled) {
                    this.bindHandlers();
                }
            },

            /** Bind event handler callbacks.
             *
             *  When ended is triggered, mark the video complete
             *  unconditionally.
             *
             *  When timeupdate is triggered, check to see if the user has
             *  passed the completeAfterTime in the video, and if so, mark the
             *  video complete.
             *
             *  When destroy is triggered, clean up outstanding resources.
             */
            bindHandlers: function() {
                var self = this;

                /** Event handler to track when video starts playing */
                this.state.el.on('play', function() {
                    self.handlePlay();
                });

                /** Event handler to track when video is paused */
                this.state.el.on('pause', function() {
                    self.handlePause();
                });

                /** Event handler to check if the video is complete, and submit
                 *  a completion if it is.
                 *
                 *  If the timeupdate handler doesn't fire after the required
                 *  percentage, this will catch any fully complete videos.
                 */
                this.state.el.on('ended.completion', function() {
                    self.handleEnded();
                });

                /** Event handler to check video progress, and mark complete if
                 *  greater than completionPercentage
                 */
                this.state.el.on('timeupdate.completion', function(ev, currentTime) {
                    self.handleTimeUpdate(currentTime);
                });

                /** Event handler to receive youtube metadata (if we even are a youtube link),
                 *  and mark complete, if youtube will insist on hosting the video itself.
                 */
                this.state.el.on('metadata_received', function() {
                    self.checkMetadata();
                });

                /** Event handler to clean up resources when the video player
                 *  is destroyed.
                 */
                this.state.el.off('destroy', this.destroy);
            },

            /** Handler to call when video starts playing */
            handlePlay: function() {
                if (!this.isCurrentlyViewing) {
                    this.isCurrentlyViewing = true;
                    this.viewingStartedAt = Date.now();
                    /* eslint-disable no-console */
                    console.log('[Video Tracking] Started tracking viewing time');
                    /* eslint-enable no-console */
                }
            },

            /** Handler to call when video is paused */
            handlePause: function() {
                if (this.isCurrentlyViewing) {
                    var sessionTime = Date.now() - this.viewingStartedAt;
                    this.totalViewingTimeMs += sessionTime;
                    this.isCurrentlyViewing = false;
                    this.viewingStartedAt = null;
                    /* eslint-disable no-console */
                    console.log('[Video Tracking] Paused. Session time: ' + (sessionTime / 1000).toFixed(1) + 's, Total: ' + (this.totalViewingTimeMs / 1000).toFixed(1) + 's');
                    /* eslint-enable no-console */
                }
            },

            /** Get total time spent viewing the video */
            getTotalViewingTime: function() {
                if (this.isCurrentlyViewing && this.viewingStartedAt) {
                    return this.totalViewingTimeMs + (Date.now() - this.viewingStartedAt);
                }
                return this.totalViewingTimeMs;
            },

            /** Handler to call when the ended event is triggered */
            handleEnded: function() {
                // Stop tracking viewing time when video ends
                if (this.isCurrentlyViewing) {
                    this.handlePause();
                }
                if (this.isComplete) {
                    return;
                }
                this.markCompletion();
            },

            /** Handler to call when a timeupdate event is triggered */
            handleTimeUpdate: function(currentTime) {
                var duration;
                if (this.isComplete) {
                    return;
                }

                // Start tracking viewing time if video is playing and we haven't started yet
                // This handles cases where 'play' event doesn't fire initially
                if (!this.isCurrentlyViewing && !this.state.videoPlayer.isPlaying) {
                    // Video is paused, don't track
                } else if (!this.isCurrentlyViewing && this.state.videoPlayer.isPlaying) {
                    // Video is playing but we haven't started tracking - start now
                    this.handlePlay();
                }

                if (this.lastSentTime !== undefined && currentTime - this.lastSentTime < this.repostDelaySeconds()) {
                    // Throttle attempts to submit in case of network issues
                    return;
                }
                if (this.completeAfterTime === undefined) {
                    // Duration is not available at initialization time
                    duration = this.state.videoPlayer.duration();
                    if (!duration) {
                        // duration is not yet set. Wait for another event,
                        // or fall back to 'ended' handler.
                        return;
                    }
                    this.completeAfterTime = this.calculateCompleteAfterTime(this.startTime, duration);
                }

                if (currentTime > this.completeAfterTime) {
                    this.markCompletion(currentTime);
                }
            },

            /** Handler to call when youtube metadata is received */
            checkMetadata: function() {
                var metadata = this.state.metadata[this.state.youtubeId()];

                // https://developers.google.com/youtube/v3/docs/videos#contentDetails.contentRating.ytRating
                if (metadata && metadata.contentRating && metadata.contentRating.ytRating === 'ytAgeRestricted') {
                    // Age-restricted videos won't play in embedded players. Instead, they ask you to watch it on
                    // youtube itself. Which means we can't notice if they complete it. Rather than leaving an
                    // incompletable video in the course, let's just mark it complete right now.
                    if (!this.isComplete) {
                        this.markCompletion();
                    }
                }
            },

            /** Submit completion to the LMS */
            markCompletion: function(currentTime) {
                var self = this;
                var errmsg;
                var timeSpentMs;
                var remainingDelayMs;

                // Mark as complete immediately to prevent multiple triggers
                if (this.isComplete) {
                    return;
                }
                this.isComplete = true;
                this.lastSentTime = currentTime;

                // Calculate how much time has been spent watching (includes current session if playing)
                timeSpentMs = this.getTotalViewingTime();

                // Calculate remaining delay: if already spent minimum time, delay is 0
                // Otherwise, delay is (minimum_time - time_already_spent)
                remainingDelayMs = Math.max(0, this.minimumViewingTimeMs - timeSpentMs);

                // Log that criteria is met with detailed info
                /* eslint-disable no-console */
                console.log('=== VIDEO COMPLETION CRITERIA MET ===');
                console.log('Currently viewing: ' + this.isCurrentlyViewing);
                console.log('Total viewing time accumulated: ' + (this.totalViewingTimeMs / 1000).toFixed(1) + 's');
                console.log('Current session time: ' + (this.isCurrentlyViewing ? ((Date.now() - this.viewingStartedAt) / 1000).toFixed(1) : 0) + 's');
                console.log('Total time spent watching: ' + (timeSpentMs / 1000).toFixed(1) + ' seconds');
                console.log('Minimum viewing time required: ' + (this.minimumViewingTimeMs / 1000) + ' seconds');
                console.log('Remaining delay before submission: ' + (remainingDelayMs / 1000).toFixed(1) + ' seconds');
                console.log('======================================');
                /* eslint-enable no-console */

                // If remaining delay is 0, log that we're submitting immediately
                if (remainingDelayMs > 0) {
                    /* eslint-disable no-console */
                    console.log('Submitting immediately - minimum viewing time already met!');
                    /* eslint-enable no-console */
                    return;
                }

                const completionSubmit = () => {
                    /* eslint-disable no-console */
                    console.log('Minimum viewing time reached. Submitting completion now...');
                    /* eslint-enable no-console */

                    self.state.el.trigger('complete');
                    if (self.state.config.publishCompletionUrl) {
                        $.ajax({
                            type: 'POST',
                            url: self.state.config.publishCompletionUrl,
                            contentType: 'application/json',
                            dataType: 'json',
                            data: JSON.stringify({completion: 1.0}),
                            success: function() {
                                self.state.el.off('timeupdate.completion');
                                self.state.el.off('ended.completion');
                                /* eslint-disable no-console */
                                console.log('Video completion successfully submitted after minimum viewing time');
                                /* eslint-enable no-console */
                            },
                            error: function(xhr) {
                                /* eslint-disable no-console */
                                self.isComplete = false;
                                errmsg = 'Failed to submit completion';
                                if (xhr.responseJSON !== undefined) {
                                    errmsg += ': ' + xhr.responseJSON.error;
                                }
                                console.warn(errmsg);
                                /* eslint-enable no-console */
                            }
                        });
                    } else {
                        /* eslint-disable no-console */
                        console.warn('publishCompletionUrl not defined');
                        /* eslint-enable no-console */
                    }
                }

                // Delay the actual API call by remaining time
                this.completionTimeoutId = setTimeout(completionSubmit, remainingDelayMs);
            },

            /** Determine what point in the video (in seconds from the
             *  beginning) counts as complete.
             */
            /**
             * Calculates the time (in seconds from the start) after which the video is considered complete.
             * - If the video duration is less than the minimum viewing time, completeAfterTime is the end of the video.
             * - Otherwise, completeAfterTime is startTime + minimum viewing time (in seconds).
             */
            calculateCompleteAfterTime: function(startTime, endTime) {
                var duration = endTime - startTime;
                var minTimeSec = this.minimumViewingTimeMs / 1000;
                if (duration < minTimeSec) {
                    return endTime;
                }
                return startTime + minTimeSec;
            },

            /**
             * Returns the remaining time (in seconds) needed to reach minimum viewing time.
             * If already met, returns 0.
             */
            getRemainingMinimumTime: function() {
                var spentSec = this.getTotalViewingTime() / 1000;
                var minTimeSec = this.minimumViewingTimeMs / 1000;
                return Math.max(0, minTimeSec - spentSec);
            },

            /** How many seconds to wait after a POST fails to try again. */
            repostDelaySeconds: function() {
                return 3.0;
            }
        };
        return VideoCompletionHandler;
    });
}(RequireJS.define));
