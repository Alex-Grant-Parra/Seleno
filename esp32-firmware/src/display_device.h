// The LovyanGFX device behind display.h, for the UI (sprites are pushed to
// it). Main loop task only, like every other display call.
#pragma once

#define LGFX_USE_V1
#include <LovyanGFX.hpp>

lgfx::LGFX_Device& displayDevice();
