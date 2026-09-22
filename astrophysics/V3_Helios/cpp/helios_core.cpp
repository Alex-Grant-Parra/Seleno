// helios_core: compiled force model and integration loop for V3 Helios.
//
// A line-for-line port of integrator.ForceModel and integrator.splitSuzuki4Step,
// which remain the readable reference (and the fallback when this module is not
// built).  No physical constants are defined here: Python passes GM, J2, the
// pole, 1/c^2 and the Suzuki weights in from constants.py / integrator.py.
//
// Build:  python astrophysics/V3_Helios/build_core.py

#include <pybind11/numpy.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <algorithm>
#include <cmath>
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

class Model {
public:
    Model(std::vector<double> mu, std::vector<double> masses, const std::string& relativity,
          int sunIdx, int oblateIdx, std::vector<int> oblateTargets, double j2Coeff,
          std::vector<double> pole, double epsSq, double invC2)
        : mu_(std::move(mu)), masses_(std::move(masses)), relativity_(parseRelativity(relativity)),
          sun_(sunIdx), oblate_(oblateIdx), targets_(std::move(oblateTargets)),
          j2Coeff_(j2Coeff), epsSq_(epsSq), invC2_(invC2) {
        n_ = static_cast<int>(mu_.size());
        if (static_cast<int>(masses_.size()) != n_) throw std::invalid_argument("mu/masses size mismatch");
        if (pole.size() != 3) throw std::invalid_argument("pole must have 3 components");
        for (int k = 0; k < 3; ++k) pole_[k] = pole[k];
        if (sun_ < 0 || sun_ >= n_) throw std::invalid_argument("sun index out of range");
        if (oblate_ >= n_) throw std::invalid_argument("oblate index out of range");
        for (int t : targets_)
            if (t < 0 || t >= n_ || t == oblate_) throw std::invalid_argument("bad oblate target");
        aNewton_.resize(3 * n_);
        potential_.resize(n_);
    }

    int size() const { return n_; }

    // Newtonian gravity (no J2).  acc must have 3n entries.
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

    // a = -(3/2) J2 GM R^2 / d^5 * [(1 - 5 z^2/d^2) d_vec + 2 z pole], plus reaction on Earth.
    void addOblateness(const double* r, double* acc) const {
        if (oblate_ < 0) return;
        const int e = oblate_;
        const double k = -j2Coeff_ * mu_[e];
        double reaction[3] = {0.0, 0.0, 0.0};
        for (int i : targets_) {
            const double d[3] = {r[3 * i] - r[3 * e], r[3 * i + 1] - r[3 * e + 1], r[3 * i + 2] - r[3 * e + 2]};
            const double dSq = dot3(d, d);
            const double z = dot3(d, pole_);
            const double c = k / (dSq * dSq * std::sqrt(dSq));
            const double radial = c * (1.0 - 5.0 * z * z / dSq);
            const double polar = 2.0 * c * z;
            const double ratio = masses_[i] / masses_[e];
            for (int a = 0; a < 3; ++a) {
                const double f = radial * d[a] + polar * pole_[a];
                acc[3 * i + a] += f;
                reaction[a] -= ratio * f;
            }
        }
        for (int a = 0; a < 3; ++a) acc[3 * e + a] += reaction[a];
    }

    void positional(const double* r, double* acc) const {
        newtonian(r, acc);
        addOblateness(r, acc);
    }

    // Relativistic (velocity-dependent) part only.
    void correction(const double* r, const double* v, double* out) {
        std::fill(out, out + 3 * n_, 0.0);
        if (relativity_ == Relativity::None) return;
        if (relativity_ == Relativity::Sun) {
            sunSchwarzschild(r, v, out);
            return;
        }
        eih(r, v, out);
    }

private:
    void sunSchwarzschild(const double* r, const double* v, double* out) const {
        const int s = sun_;
        const double muSun = mu_[s];
        double momentum[3] = {0.0, 0.0, 0.0};
        for (int i = 0; i < n_; ++i) {
            if (i == s) continue;
            const double rv[3] = {r[3 * i] - r[3 * s], r[3 * i + 1] - r[3 * s + 1], r[3 * i + 2] - r[3 * s + 2]};
            const double vv[3] = {v[3 * i] - v[3 * s], v[3 * i + 1] - v[3 * s + 1], v[3 * i + 2] - v[3 * s + 2]};
            const double inv = 1.0 / std::sqrt(dot3(rv, rv) + epsSq_);
            const double vSq = dot3(vv, vv);
            const double rDotV = dot3(rv, vv);
            const double pre = muSun * invC2_ * inv * inv * inv;
            const double cr = 4.0 * muSun * inv - vSq;
            for (int a = 0; a < 3; ++a) {
                out[3 * i + a] = pre * (cr * rv[a] + 4.0 * rDotV * vv[a]);
                momentum[a] += masses_[i] * out[3 * i + a];
            }
        }
        // Equal and opposite reaction on the Sun keeps total momentum conserved.
        for (int a = 0; a < 3; ++a) out[3 * s + a] = -momentum[a] / masses_[s];
    }

    // Einstein-Infeld-Hoffmann 1PN (beta = gamma = 1); see integrator.ForceModel._eih.
    void eih(const double* r, const double* v, double* out) {
        double* aN = aNewton_.data();
        double* U = potential_.data();
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
            for (int a = 0; a < 3; ++a) out[3 * i + a] = t[a] * invC2_;
        }
    }

    std::vector<double> mu_, masses_;
    Relativity relativity_;
    int n_ = 0, sun_ = 0, oblate_ = -1;
    std::vector<int> targets_;
    double j2Coeff_ = 0.0, pole_[3] = {0.0, 0.0, 1.0}, epsSq_ = 0.0, invC2_ = 0.0;
    std::vector<double> aNewton_, potential_;   // scratch for eih()
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

// Integrate `steps` split-Suzuki steps.  Returns (r_hist, v_hist, r_final,
// v_final); a sample is stored after every `store_every` steps.  The
// (positional, correction) state is recomputed from (r, v) at the start, which
// is exactly what the previous call would have carried, so chunked calls give
// the same result as one long call.
py::tuple integrateSplitSuzuki(Model& model, const Array& r0, const Array& v0, double dt,
                               long long steps, long long storeEvery, std::vector<double> weights) {
    const int n = model.size();
    if (steps < 0 || storeEvery < 1) throw std::invalid_argument("steps must be >= 0, store_every >= 1");
    std::vector<double> r = stateVector(r0, n, "r"), v = stateVector(v0, n, "v");
    const long long nStore = steps / storeEvery;
    Array rHist({static_cast<py::ssize_t>(nStore), static_cast<py::ssize_t>(n), static_cast<py::ssize_t>(3)});
    Array vHist({static_cast<py::ssize_t>(nStore), static_cast<py::ssize_t>(n), static_cast<py::ssize_t>(3)});
    double* rOut = rHist.mutable_data();
    double* vOut = vHist.mutable_data();

    {
        py::gil_scoped_release release;
        std::vector<double> aPos(3 * n), corr(3 * n);
        model.positional(r.data(), aPos.data());
        model.correction(r.data(), v.data(), corr.data());
        const int m = 3 * n;
        long long stored = 0;
        for (long long step = 0; step < steps; ++step) {
            for (int k = 0; k < m; ++k) v[k] += (0.5 * dt) * corr[k];
            for (double w : weights) {
                const double h = w * dt;
                for (int k = 0; k < m; ++k) {
                    v[k] += (0.5 * h) * aPos[k];
                    r[k] += h * v[k];
                }
                model.positional(r.data(), aPos.data());
                for (int k = 0; k < m; ++k) v[k] += (0.5 * h) * aPos[k];
            }
            model.correction(r.data(), v.data(), corr.data());
            for (int k = 0; k < m; ++k) v[k] += (0.5 * dt) * corr[k];

            if ((step + 1) % storeEvery == 0) {
                std::copy(r.begin(), r.end(), rOut + stored * m);
                std::copy(v.begin(), v.end(), vOut + stored * m);
                ++stored;
            }
        }
    }
    return py::make_tuple(rHist, vHist, toArray(r, n), toArray(v, n));
}

// Single force evaluations, for checking against the Python ForceModel.
Array evalPositional(const Model& model, const Array& r) {
    const int n = model.size();
    std::vector<double> rv = stateVector(r, n, "r"), out(3 * n);
    model.positional(rv.data(), out.data());
    return toArray(out, n);
}

Array evalCorrection(Model& model, const Array& r, const Array& v) {
    const int n = model.size();
    std::vector<double> rv = stateVector(r, n, "r"), vv = stateVector(v, n, "v"), out(3 * n);
    model.correction(rv.data(), vv.data(), out.data());
    return toArray(out, n);
}

}  // namespace

PYBIND11_MODULE(helios_core, m) {
    m.doc() = "Compiled force model and split-Suzuki integrator for V3 Helios";
    py::class_<Model>(m, "Model")
        .def(py::init<std::vector<double>, std::vector<double>, const std::string&, int, int,
                      std::vector<int>, double, std::vector<double>, double, double>(),
             py::arg("mu"), py::arg("masses"), py::arg("relativity"), py::arg("sun_idx"),
             py::arg("oblate_idx"), py::arg("oblate_targets"), py::arg("j2_coeff"),
             py::arg("pole"), py::arg("eps_sq"), py::arg("inv_c2"))
        .def_property_readonly("size", &Model::size);
    m.def("positional", &evalPositional, py::arg("model"), py::arg("r"));
    m.def("correction", &evalCorrection, py::arg("model"), py::arg("r"), py::arg("v"));
    m.def("integrate_split_suzuki", &integrateSplitSuzuki, py::arg("model"), py::arg("r"),
          py::arg("v"), py::arg("dt"), py::arg("steps"), py::arg("store_every"), py::arg("weights"));
}
