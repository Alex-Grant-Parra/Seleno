// Coordinated two-axis motion along a polyline: slews that curve round
// something (the Sun) without stopping at each corner. Pure arithmetic so
// it can be unit tested on the host.
//
// Units are whatever the caller uses for both axes (the firmware uses 1/256
// microsteps). A segment's length is the larger of its two axis distances,
// and speeds are for the faster axis, so neither motor goes over the
// cruise speed and a straight segment is a linear move of both axes.
//
// Corners are taken without stopping: the speed through a corner is limited
// so that neither axis's speed jumps by more than `jump` (the pull-in speed
// a motor can change by instantly). A gently curving path (small turns at
// each point) runs at cruise speed throughout; a sharp reversal slows to
// jump / 2. The path starts and ends at the jump speed, like a single move.
#pragma once

#include <cmath>
#include <cstdint>

namespace seleno {

constexpr int kMaxPathPoints = 64;  // the start included

struct PathPlan {
  int count = 0;  // points, the start (0, 0) included
  double x[kMaxPathPoints];
  double y[kMaxPathPoints];
  double ux[kMaxPathPoints];      // per segment k (point k to k+1): axis speed / path speed, -1..1
  double uy[kMaxPathPoints];
  double len[kMaxPathPoints];     // per segment
  double at[kMaxPathPoints];      // path distance at each point
  double vmaxAt[kMaxPathPoints];  // fastest speed through each point (after braking for later ones)
  double total = 0.0;
  double cruise = 0.0;
  double accel = 0.0;
  double jump = 0.0;
};

struct PathCursor {
  int seg = 0;      // segment being followed
  double s = 0.0;   // distance along the path
  double v = 0.0;   // path speed
  bool done = false;
};

// Plans a path through n points relative to the start (which is (0, 0) and
// not in the list). Repeated points are dropped. Returns false if there are
// too many points, nothing to move, or a speed isn't positive.
inline bool planPath(PathPlan& p, const double* xs, const double* ys, int n, double cruise, double accel,
                     double jump) {
  if (n < 1 || n > kMaxPathPoints - 1 || !(cruise > 0.0) || !(jump > 0.0)) {
    return false;
  }
  p.count = 1;
  p.x[0] = 0.0;
  p.y[0] = 0.0;
  for (int i = 0; i < n; i++) {
    double dx = xs[i] - p.x[p.count - 1];
    double dy = ys[i] - p.y[p.count - 1];
    if (std::fabs(dx) < 0.5 && std::fabs(dy) < 0.5) {
      continue;
    }
    p.x[p.count] = xs[i];
    p.y[p.count] = ys[i];
    p.count++;
  }
  if (p.count < 2) {
    return false;
  }
  p.cruise = cruise;
  p.accel = accel > 0.0 ? accel : 1e12;  // no ramp: speed changes at once
  p.jump = jump;
  p.at[0] = 0.0;
  for (int k = 0; k + 1 < p.count; k++) {
    double dx = p.x[k + 1] - p.x[k];
    double dy = p.y[k + 1] - p.y[k];
    double len = std::fabs(dx) > std::fabs(dy) ? std::fabs(dx) : std::fabs(dy);
    p.len[k] = len;
    p.ux[k] = dx / len;
    p.uy[k] = dy / len;
    p.at[k + 1] = p.at[k] + len;
  }
  p.total = p.at[p.count - 1];
  double startSpeed = jump < cruise ? jump : cruise;
  p.vmaxAt[0] = startSpeed;
  p.vmaxAt[p.count - 1] = startSpeed;
  for (int k = 1; k + 1 < p.count; k++) {
    double dux = std::fabs(p.ux[k] - p.ux[k - 1]);
    double duy = std::fabs(p.uy[k] - p.uy[k - 1]);
    double change = dux > duy ? dux : duy;
    double v = change > 1e-9 ? jump / change : cruise;
    p.vmaxAt[k] = v < cruise ? v : cruise;
  }
  // Brake in time for every later corner and the end.
  for (int k = p.count - 2; k >= 0; k--) {
    double reach = std::sqrt(p.vmaxAt[k + 1] * p.vmaxAt[k + 1] + 2.0 * p.accel * p.len[k]);
    if (reach < p.vmaxAt[k]) {
      p.vmaxAt[k] = reach;
    }
  }
  return true;
}

inline void pathBegin(const PathPlan& p, PathCursor& c) {
  c.seg = 0;
  c.s = 0.0;
  c.v = p.vmaxAt[0];
  c.done = false;
}

// Advances the cursor by dt. Stopping slows to a halt along the path;
// returns true once it's slow enough to stop dead (the jump speed).
inline bool pathStep(const PathPlan& p, PathCursor& c, double dt, bool stopping) {
  if (c.done) {
    c.v = 0.0;
    return stopping;
  }
  double remaining = p.at[c.seg + 1] - c.s;
  if (remaining < 0.0) remaining = 0.0;
  double cap = std::sqrt(p.vmaxAt[c.seg + 1] * p.vmaxAt[c.seg + 1] + 2.0 * p.accel * remaining);
  if (cap > p.cruise) cap = p.cruise;
  double target = stopping ? 0.0 : cap;
  double dv = p.accel * dt;
  if (c.v < target) {
    c.v = c.v + dv > target ? target : c.v + dv;
  } else if (c.v > target) {
    c.v = c.v - dv < target ? target : c.v - dv;
  }
  if (c.v > cap) c.v = cap;
  double floorSpeed = p.jump < p.cruise ? p.jump : p.cruise;
  if (stopping && c.v <= floorSpeed) {
    return true;
  }
  c.s += c.v * dt;
  while (c.seg < p.count - 2 && c.s >= p.at[c.seg + 1]) {
    c.seg++;
  }
  if (c.s >= p.total) {
    c.s = p.total;
    c.done = true;
  }
  return false;
}

// Where the cursor is, and each axis's speed there (signed).
inline void pathPoint(const PathPlan& p, const PathCursor& c, double& x, double& y, double& vx, double& vy) {
  int k = c.seg;
  double t = c.s - p.at[k];
  x = p.x[k] + p.ux[k] * t;
  y = p.y[k] + p.uy[k] * t;
  if (c.done) {
    x = p.x[p.count - 1];
    y = p.y[p.count - 1];
    vx = vy = 0.0;
    return;
  }
  vx = c.v * p.ux[k];
  vy = c.v * p.uy[k];
}

// A rough time to the end at cruise speed, for the panel.
inline double pathEtaSeconds(const PathPlan& p, const PathCursor& c) {
  if (c.done) return 0.0;
  return (p.total - c.s) / p.cruise + p.cruise / (2.0 * p.accel);
}

}  // namespace seleno
