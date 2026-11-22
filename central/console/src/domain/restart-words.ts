/**
 * Restart app in words (decision 0036): each step, each way it can fail and each reason Central
 * does not take the request, an exhaustive `Record` over the generated contract, so a code Central
 * adds (central/fleet/app_restart.py) and this module does not word fails the typecheck. Pure.
 */
import type { AppRestartRefusal, RestartFailure, RestartStepName } from "../generated/contract.ts";

/** Each step of a restart, as its line says it. */
export const RESTART_STEPS: Record<RestartStepName, string> = {
  requested: "Requested",
  app_stopped: "App stopped",
  app_started: "App started",
  showing: "Showing",
};
export const RESTART_ORDER: readonly RestartStepName[] = ["requested", "app_stopped", "app_started", "showing"];

/** Why a restart did not finish, in plain words naming the part. */
export const RESTART_FAILURES: Record<RestartFailure, string> = {
  not_delivered: "The request did not reach the Pi: it has no connection to Central, or it did not answer.",
  in_flight: "The Pi was already restarting the photo app for another request.",
  not_stopped: "The Pi could not stop the photo app, so nothing was started.",
  not_started: "The photo app was stopped and did not start again.",
  timed_out: "The photo app did not report a picture in time after the restart.",
};

/** Why Central did not take a restart request, in plain words: every code Central can answer. */
export const RESTART_REFUSALS: Record<AppRestartRefusal, string> = {
  node_app_restart_outstanding: "Another restart of this Pi's photo app is already on its way.",
  node_device_unavailable: "Central does not know this Pi any more.",
  node_app_restart_unavailable: "Central cannot send requests to Pis right now.",
  invalid_operator_app_restart: "Central could not read the request.",
};
