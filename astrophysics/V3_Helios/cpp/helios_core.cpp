// helios_core: compiled force model and integrator for V3 Helios.
//
// A line-for-line port of integrator.py (Physics, integratePython,
// propagatePython), which stays the readable reference and the fallback when
// this module is not built.  No physical constants are defined here: Python
// passes every value in (see integrator.compiledModel), so constants.py stays
// the single source.
//
// Thread safety: a Model is immutable after construction and each call gets
// its own Workspace, so one Model can serve concurrent calls (the GIL is
// released while integrating).
//
// Build:  python astrophysics/V3_Helios/build_core.py

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace py = pybind11;

namespace {

enum class Relativity { None, Sun, Eih };

Relativity parseRelativity(const std::string& name) {
    if (name == "none") return Relativity::None;
    if (name == "sun") return Relativity::Sun;
    if (name == "eih") return Relativity::Eih;
    throw std::invalid_argument("relativity must be 'none', 'sun' or 'eih'");
}

inline double dot3(const double* a, const double* b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

// Per-call mutable state: the pole in use, the accelerations at the current
// (r, v), and scratch space for the relativistic terms.
struct Workspace {
    long long poleIndex = std::numeric_limits<long long>::min();
    double pole[3] = {0.0, 0.0, 1.0};
    std::vector<double> aPos, corr, aNewton, potential;

    explicit Workspace(int n) : aPos(3 * n), corr(3 * n), aNewton(3 * n), potential(n) {}
};

class Model {
public:
    Model(std::vector<double> mu, std::vector<double> masses, const std::string& relativity,
          int sunIdx, int earthIdx, int moonIdx, bool earthJ2, bool lunarFigure, bool tides,
          double j2Coeff, double lunarFigureCoeff, double tideK2R5, double tidalLag, double earthRate,
          double epsSq, double invC2, double et0, double poleInterval, double obliquity0, std::vector<double> psiCoeffs,
          std::vector<double> omegaCoeffs, double secondsPerCentury, double arcsecToRad,
          std::vector<double> weights)
        : mu_(std::move(mu)), masses_(std::move(masses)), relativity_(parseRelativity(relativity)),
          sun_(sunIdx), earth_(earthIdx), moon_(moonIdx), earthJ2_(earthJ2),
          lunarFigure_(lunarFigure), tides_(tides), j2Coeff_(j2Coeff),
          lunarFigureCoeff_(lunarFigureCoeff), tideK2R5_(tideK2R5), tidalLag_(tidalLag),
          earthRate_(earthRate), epsSq_(epsSq), invC2_(invC2), et0_(et0), poleInterval_(poleInterval),
          obliquity0_(obliquity0), psi_(std::move(psiCoeffs)), omega_(std::move(omegaCoeffs)),
          secondsPerCentury_(secondsPerCentury), arcsecToRad_(arcsecToRad),
          weights_(std::move(weights)) {
        n_ = static_cast<int>(mu_.size());
        if (static_cast<int>(masses_.size()) != n_) throw std::invalid_argument("mu/masses size mismatch");
        if (sun_ < 0 || sun_ >= n_) throw std::invalid_argument("sun index out of range");
        const bool earthMoon = earthJ2_ || lunarFigure_ || tides_;
        if (earthMoon && (earth_ < 0 || earth_ >= n_ || moon_ < 0 || moon_ >= n_ || earth_ == moon_))
            throw std::invalid_argument("J2/lunar figure/tides need valid, distinct Earth and Moon indices");
        if (psi_.empty() || omega_.empty()) throw std::invalid_argument("precession coefficients missing");
        if (weights_.empty()) throw std::invalid_argument("integrator weights missing");
        if (earthMoon) moonToEarthMass_ = masses_[moon_] / masses_[earth_];
    }

    int size() const { return n_; }

    // ---- Earth's pole (see Physics.poleIndex / poleForIndex) -------------------
    long long poleIndex(double t, double h) const {
        if (poleInterval_ <= 0.0) return 0;
        return static_cast<long long>(std::floor((t + 0.5 * h) / poleInterval_));
    }

    void poleForIndex(long long j, double* pole) const {
        const double tMid = poleInterval_ > 0.0 ? (static_cast<double>(j) + 0.5) * poleInterval_ : 0.0;
        const double T = (et0_ + tMid) / secondsPerCentury_;
        const double psi = horner(psi_, T) * arcsecToRad_;
        const double omega = obliquity0_ + horner(omega_, T) * arcsecToRad_;
        pole[0] = std::sin(omega) * std::sin(psi);
        pole[1] = std::sin(omega) * std::cos(psi);
        pole[2] = std::cos(omega);
    }

    // ---- Forces -----------------------------------------------------------------
    void newtonian(const double* r, double* acc) const {
        std::fill(acc, acc + 3 * n_, 0.0);
        for (int i = 0; i < n_; ++i) {
            for (int j = i + 1; j < n_; ++j) {
                const double dx = r[3 * i] - r[3 * j];
                const double dy = r[3 * i + 1] - r[3 * j + 1];
                const double dz = r[3 * i + 2] - r[3 * j + 2];
                const double dSq = dx * dx + dy * dy + dz * dz + epsSq_;
                const double inv3 = 1.0 / (dSq * std::sqrt(dSq));
                const double wi = mu_[j] * inv3;   // pull on i towards j
                const double wj = mu_[i] * inv3;   // pull on j towards i
                acc[3 * i] -= wi * dx;
                acc[3 * i + 1] -= wi * dy;
                acc[3 * i + 2] -= wi * dz;
                acc[3 * j] += wj * dx;
                acc[3 * j + 1] += wj * dy;
                acc[3 * j + 2] += wj * dz;
            }
        }
    }

    // Newtonian + Earth J2 on the Moon + the Moon's figure.
    void positional(const double* r, const double* pole, double* acc) const {
        newtonian(r, acc);
        if (earthJ2_) addEarthJ2(r, pole, acc);
        if (lunarFigure_) addLunarFigure(r, acc);
    }

    void addEarthJ2(const double* r, const double* pole, double* acc) const {
        // a = -(3/2) J2 GM R^2 / d^5 * [(1 - 5 z^2/d^2) d_vec + 2 z pole], reaction on the Earth.
        const int e = earth_, m = moon_;
        const double d[3] = {r[3 * m] - r[3 * e], r[3 * m + 1] - r[3 * e + 1], r[3 * m + 2] - r[3 * e + 2]};
        const double dSq = dot3(d, d);
        const double z = dot3(d, pole);
        const double c = -j2Coeff_ * mu_[e] / (dSq * dSq * std::sqrt(dSq));
        const double radial = c * (1.0 - 5.0 * z * z / dSq);
        const double polar = 2.0 * c * z;
        for (int a = 0; a < 3; ++a) {
            const double f = radial * d[a] + polar * pole[a];
            acc[3 * m + a] += f;
            acc[3 * e + a] -= moonToEarthMass_ * f;
        }
    }

    // a_earth = -(3/2 J2 + 9 C22) GM_moon R^2 / d^5 * d_vec (d_vec = Earth - Moon), reaction on the
    // Moon; see integrator.Physics._addLunarFigure.
    void addLunarFigure(const double* r, double* acc) const {
        const int e = earth_, m = moon_;
        const double d[3] = {r[3 * e] - r[3 * m], r[3 * e + 1] - r[3 * m + 1], r[3 * e + 2] - r[3 * m + 2]};
        const double dSq = dot3(d, d);
        const double c = -lunarFigureCoeff_ / (dSq * dSq * std::sqrt(dSq));
        const double ce = c * mu_[m], cm = -c * mu_[e];
        for (int a = 0; a < 3; ++a) {
            acc[3 * e + a] += ce * d[a];
            acc[3 * m + a] += cm * d[a];
        }
    }

    // Relativity + tides.
    void correction(Workspace& ws, const double* r, const double* v, const double* pole, double* out) const {
        std::fill(out, out + 3 * n_, 0.0);
        if (relativity_ == Relativity::Sun) sunSchwarzschild(r, v, out);
        if (relativity_ == Relativity::Eih) eih(ws, r, v, out);
        if (tides_) addTides(r, v, pole, out);
    }

    // ---- Integrator (see integrator._ensurePole / _step) ---------------------------
    void ensurePole(Workspace& ws, const double* r, const double* v, double t, double h) const {
        const long long j = poleIndex(t, h);
        if (j == ws.poleIndex) return;
        ws.poleIndex = j;
        poleForIndex(j, ws.pole);
        positional(r, ws.pole, ws.aPos.data());
        correction(ws, r, v, ws.pole, ws.corr.data());
    }

    void step(Workspace& ws, double* r, double* v, double h) const {
        const int m = 3 * n_;
        for (int k = 0; k < m; ++k) v[k] += (0.5 * h) * ws.corr[k];
        for (double w : weights_) {
            const double s = w * h;
            for (int k = 0; k < m; ++k) {
                v[k] += (0.5 * s) * ws.aPos[k];
                r[k] += s * v[k];
            }
            positional(r, ws.pole, ws.aPos.data());
            for (int k = 0; k < m; ++k) v[k] += (0.5 * s) * ws.aPos[k];
        }
        correction(ws, r, v, ws.pole, ws.corr.data());
        for (int k = 0; k < m; ++k) v[k] += (0.5 * h) * ws.corr[k];
    }

private:
    static double horner(const std::vector<double>& c, double T) {
        double value = 0.0;
        for (auto it = c.rbegin(); it != c.rend(); ++it) value = value * T + *it;
        return value;
    }

    void sunSchwarzschild(const double* r, const double* v, double* out) const {
        const int s = sun_;
        const double muSun = mu_[s];
        double momentum[3] = {0.0, 0.0, 0.0};
        for (int i = 0; i < n_; ++i) {
            if (i == s) continue;
            const double rv[3] = {r[3 * i] - r[3 * s], r[3 * i + 1] - r[3 * s + 1], r[3 * i + 2] - r[3 * s + 2]};
            const double vv[3] = {v[3 * i] - v[3 * s], v[3 * i + 1] - v[3 * s + 1], v[3 * i + 2] - v[3 * s + 2]};
            const double inv = 1.0 / std::sqrt(dot3(rv, rv) + epsSq_);
            const double pre = muSun * invC2_ * inv * inv * inv;
            const double cr = 4.0 * muSun * inv - dot3(vv, vv);
            const double rDotV = dot3(rv, vv);
            for (int a = 0; a < 3; ++a) {
                out[3 * i + a] += pre * (cr * rv[a] + 4.0 * rDotV * vv[a]);
                momentum[a] += masses_[i] * out[3 * i + a];
            }
        }
        // Equal and opposite reaction on the Sun keeps total momentum conserved.
        for (int a = 0; a < 3; ++a) out[3 * s + a] = -momentum[a] / masses_[s];
    }

    // Einstein-Infeld-Hoffmann 1PN (beta = gamma = 1); see integrator.Physics._eih.
    void eih(Workspace& ws, const double* r, const double* v, double* out) const {
        double* aN = ws.aNewton.data();
        double* U = ws.potential.data();
        newtonian(r, aN);
        for (int i = 0; i < n_; ++i) {
            double sum = 0.0;
            for (int k = 0; k < n_; ++k) {
                if (k == i) continue;
                const double dx = r[3 * i] - r[3 * k];
                const double dy = r[3 * i + 1] - r[3 * k + 1];
                const double dz = r[3 * i + 2] - r[3 * k + 2];
                sum += mu_[k] / std::sqrt(dx * dx + dy * dy + dz * dz + epsSq_);
            }
            U[i] = sum;
        }
        for (int i = 0; i < n_; ++i) {
            const double* vi = v + 3 * i;
            const double viSq = dot3(vi, vi);
            double t[3] = {0.0, 0.0, 0.0};
            for (int j = 0; j < n_; ++j) {
                if (j == i) continue;
                const double* vj = v + 3 * j;
                const double* aj = aN + 3 * j;
                const double d[3] = {r[3 * i] - r[3 * j], r[3 * i + 1] - r[3 * j + 1], r[3 * i + 2] - r[3 * j + 2]};
                const double invR = 1.0 / std::sqrt(dot3(d, d) + epsSq_);
                const double w = mu_[j] * invR * invR * invR;
                const double dDotVj = dot3(d, vj);
                const double dDotVi = dot3(d, vi);
                const double dDotAj = dot3(d, aj);
                const double q = dDotVj * invR;
                const double B = -4.0 * U[i] - U[j] + viSq + 2.0 * dot3(vj, vj) - 4.0 * dot3(vi, vj)
                                 - 1.5 * q * q - 0.5 * dDotAj;
                const double w2 = w * (4.0 * dDotVi - 3.0 * dDotVj);
                const double w3 = 3.5 * mu_[j] * invR;
                for (int a = 0; a < 3; ++a)
                    t[a] += -(w * B) * d[a] + w2 * (vi[a] - vj[a]) + w3 * aj[a];
            }
            for (int a = 0; a < 3; ++a) out[3 * i + a] += t[a] * invC2_;
        }
    }

    // a_moon = -3 k2 GM_moon R^5 / d^8 * [d + dt (2 (d.w)/d^2 d + d x Omega + w)]  (Mignard 1979)
    void addTides(const double* r, const double* v, const double* pole, double* out) const {
        const int e = earth_, m = moon_;
        const double d[3] = {r[3 * m] - r[3 * e], r[3 * m + 1] - r[3 * e + 1], r[3 * m + 2] - r[3 * e + 2]};
        const double w[3] = {v[3 * m] - v[3 * e], v[3 * m + 1] - v[3 * e + 1], v[3 * m + 2] - v[3 * e + 2]};
        const double o[3] = {earthRate_ * pole[0], earthRate_ * pole[1], earthRate_ * pole[2]};
        const double dSq = dot3(d, d);
        const double radial = 2.0 * dot3(d, w) / dSq;
        const double c = -3.0 * tideK2R5_ * mu_[m] / (dSq * dSq * dSq * dSq);
        const double cross[3] = {d[1] * o[2] - d[2] * o[1], d[2] * o[0] - d[0] * o[2], d[0] * o[1] - d[1] * o[0]};
        for (int a = 0; a < 3; ++a) {
            const double f = c * (d[a] + tidalLag_ * (radial * d[a] + cross[a] + w[a]));
            out[3 * m + a] += f;
            out[3 * e + a] -= moonToEarthMass_ * f;
        }
    }

    std::vector<double> mu_, masses_;
    Relativity relativity_;
    int n_ = 0, sun_ = 0, earth_ = -1, moon_ = -1;
    bool earthJ2_ = false, lunarFigure_ = false, tides_ = false;
    double j2Coeff_, lunarFigureCoeff_, tideK2R5_, tidalLag_, earthRate_, epsSq_, invC2_;
    double et0_, poleInterval_, obliquity0_;
    std::vector<double> psi_, omega_;
    double secondsPerCentury_, arcsecToRad_;
    std::vector<double> weights_;
    double moonToEarthMass_ = 0.0;
};

using Array = py::array_t<double, py::array::c_style | py::array::forcecast>;

std::vector<double> stateVector(const Array& arr, int n, const char* what) {
    if (arr.ndim() != 2 || arr.shape(0) != n || arr.shape(1) != 3)
        throw std::invalid_argument(std::string(what) + " must have shape (n_bodies, 3)");
    return std::vector<double>(arr.data(), arr.data() + 3 * n);
}

Array toArray(const std::vector<double>& flat, int n) {
    Array out({n, 3});
    std::copy(flat.begin(), flat.end(), out.mutable_data());
    return out;
}

Array historyArray(long long rows, int n) {
    return Array({static_cast<py::ssize_t>(rows), static_cast<py::ssize_t>(n), static_cast<py::ssize_t>(3)});
}

// `steps` steps of signed size h from time t0 (see integrator.integratePython).
// Returns (r_hist, v_hist, r, v) with a sample after every `store_every` steps.
py::tuple integrate(const Model& model, const Array& r0, const Array& v0, double t0, double h,
                    long long steps, long long storeEvery) {
    const int n = model.size();
    if (steps < 0 || storeEvery < 1) throw std::invalid_argument("steps must be >= 0, store_every >= 1");
    if (h == 0.0) throw std::invalid_argument("step size must be non-zero");
    std::vector<double> r = stateVector(r0, n, "r"), v = stateVector(v0, n, "v");
    const long long nStore = steps / storeEvery;
    Array rHist = historyArray(nStore, n), vHist = historyArray(nStore, n);
    double* rOut = rHist.mutable_data();
    double* vOut = vHist.mutable_data();
    {
        py::gil_scoped_release release;
        Workspace ws(n);
        const int m = 3 * n;
        long long stored = 0;
        for (long long k = 0; k < steps; ++k) {
            const double t = t0 + static_cast<double>(k) * h;
            model.ensurePole(ws, r.data(), v.data(), t, h);
            model.step(ws, r.data(), v.data(), h);
            if ((k + 1) % storeEvery == 0) {
                std::copy(r.begin(), r.end(), rOut + stored * m);
                std::copy(v.begin(), v.end(), vOut + stored * m);
                ++stored;
            }
        }
    }
    return py::make_tuple(rHist, vHist, toArray(r, n), toArray(v, n));
}

// States at `targets` (all on one side of t0, nearest first) from (r, v) at t0,
// stepping h_abs on the build grid then one partial step to each target
// (see integrator.propagatePython).  Returns (r_out, v_out).
py::tuple propagate(const Model& model, const Array& r0, const Array& v0, double t0, double hAbs,
                    std::vector<double> targets) {
    const int n = model.size();
    if (!(hAbs > 0.0)) throw std::invalid_argument("h_abs must be positive");
    int sign = 0;
    for (size_t i = 0; i < targets.size(); ++i) {
        const int s = (targets[i] > t0) - (targets[i] < t0);
        if (s != 0 && sign != 0 && s != sign) throw std::invalid_argument("targets must lie on one side of t0");
        if (s != 0) sign = s;
        if (i > 0 && std::fabs(targets[i] - t0) < std::fabs(targets[i - 1] - t0))
            throw std::invalid_argument("targets must be sorted nearest first");
    }
    std::vector<double> r = stateVector(r0, n, "r"), v = stateVector(v0, n, "v");
    Array rOutArr = historyArray(static_cast<long long>(targets.size()), n);
    Array vOutArr = historyArray(static_cast<long long>(targets.size()), n);
    double* rOut = rOutArr.mutable_data();
    double* vOut = vOutArr.mutable_data();
    {
        py::gil_scoped_release release;
        const double h = (sign > 0 ? 1.0 : -1.0) * hAbs;
        const int m = 3 * n;
        Workspace ws(n);
        long long done = 0;
        for (size_t idx = 0; idx < targets.size(); ++idx) {
            const long long nTarget = static_cast<long long>(std::floor(std::fabs(targets[idx] - t0) / hAbs));
            while (done < nTarget) {
                const double t = t0 + static_cast<double>(done) * h;
                model.ensurePole(ws, r.data(), v.data(), t, h);
                model.step(ws, r.data(), v.data(), h);
                ++done;
            }
            const double tGrid = t0 + static_cast<double>(done) * h;
            const double rem = targets[idx] - tGrid;
            if (rem != 0.0) {
                Workspace part = ws;
                std::vector<double> rp = r, vp = v;
                model.ensurePole(part, rp.data(), vp.data(), tGrid, rem);
                model.step(part, rp.data(), vp.data(), rem);
                std::copy(rp.begin(), rp.end(), rOut + idx * m);
                std::copy(vp.begin(), vp.end(), vOut + idx * m);
            } else {
                std::copy(r.begin(), r.end(), rOut + idx * m);
                std::copy(v.begin(), v.end(), vOut + idx * m);
            }
        }
    }
    return py::make_tuple(rOutArr, vOutArr);
}

// Single force evaluations, for checking against integrator.Physics.
Array evalPositional(const Model& model, const Array& r, const std::vector<double>& pole) {
    const int n = model.size();
    if (pole.size() != 3) throw std::invalid_argument("pole must have 3 components");
    std::vector<double> rv = stateVector(r, n, "r"), out(3 * n);
    model.positional(rv.data(), pole.data(), out.data());
    return toArray(out, n);
}

Array evalCorrection(const Model& model, const Array& r, const Array& v, const std::vector<double>& pole) {
    const int n = model.size();
    if (pole.size() != 3) throw std::invalid_argument("pole must have 3 components");
    std::vector<double> rv = stateVector(r, n, "r"), vv = stateVector(v, n, "v"), out(3 * n);
    Workspace ws(n);
    model.correction(ws, rv.data(), vv.data(), pole.data(), out.data());
    return toArray(out, n);
}

std::vector<double> evalPole(const Model& model, long long j) {
    std::vector<double> pole(3);
    model.poleForIndex(j, pole.data());
    return pole;
}

}  // namespace

PYBIND11_MODULE(helios_core, m) {
    m.doc() = "Compiled force model and integrator for V3 Helios (port of integrator.py)";
    py::class_<Model>(m, "Model")
        .def(py::init<std::vector<double>, std::vector<double>, const std::string&, int, int, int, bool,
                      bool, bool, double, double, double, double, double, double, double, double, double,
                      double,
                      std::vector<double>, std::vector<double>, double, double, std::vector<double>>(),
             py::arg("mu"), py::arg("masses"), py::arg("relativity"), py::arg("sun_idx"),
             py::arg("earth_idx"), py::arg("moon_idx"), py::arg("earth_j2"), py::arg("lunar_figure"),
             py::arg("tides"), py::arg("j2_coeff"), py::arg("lunar_figure_coeff"), py::arg("tide_k2r5"), py::arg("tidal_lag"), py::arg("earth_rate"),
             py::arg("eps_sq"), py::arg("inv_c2"), py::arg("et0"), py::arg("pole_interval"),
             py::arg("obliquity0"), py::arg("psi_coeffs"), py::arg("omega_coeffs"),
             py::arg("seconds_per_century"), py::arg("arcsec_to_rad"), py::arg("weights"))
        .def_property_readonly("size", &Model::size);
    m.def("integrate", &integrate, py::arg("model"), py::arg("r"), py::arg("v"), py::arg("t0"),
          py::arg("h"), py::arg("steps"), py::arg("store_every"));
    m.def("propagate", &propagate, py::arg("model"), py::arg("r"), py::arg("v"), py::arg("t0"),
          py::arg("h_abs"), py::arg("targets"));
    m.def("positional", &evalPositional, py::arg("model"), py::arg("r"), py::arg("pole"));
    m.def("correction", &evalCorrection, py::arg("model"), py::arg("r"), py::arg("v"), py::arg("pole"));
    m.def("pole", &evalPole, py::arg("model"), py::arg("index"));
}
