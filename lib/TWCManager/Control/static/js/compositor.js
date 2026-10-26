// Vendored from MikeBishop/MMM-Powerwall@41b318ee69a5c5b08b6c30bce9074b1428c91e43
// (compositor.js). Change it there and copy it here.
/* Magic Mirror
 * Module: MMM-Powerwall
 *
 * Builds Tesla compositor image URLs from the vehicle_config values the
 * Tesla API (or Teslamate) reports.
 *
 * By Mike Bishop
 * MIT Licensed.
 */

const TeslaCompositor = (function () {
	const LEGACY_COMPOSITOR = "https://static-assets.tesla.com/v1/compositor/";
	const CONFIGURATOR_COMPOSITOR = "https://static-assets.tesla.com/configurator/compositor";

	const MODEL_CODES = {
		"models": "ms",
		"models2": "ms",
		"lychee": "ms",
		"model3": "m3",
		"modelx": "mx",
		"tamarind": "mx",
		"modely": "my",
		"cybertruck": "ct",
	};

	// Keys are normalized:  lowercase, alphanumerics only
	const PAINT_CODES = {
		"white": "PPSW",
		"pearl": "PPSW",
		"pearlwhite": "PPSW",
		"pearlwhitemulticoat": "PPSW",
		"solidwhite": "PBCW",
		"black": "PBSB",
		"solidblack": "PBSB",
		"obsidianblack": "PMBL",
		"metallicblack": "PMBL",
		"midnightsilver": "PMNG",
		"midnightsilvermetallic": "PMNG",
		"steelgrey": "PMNG",
		"silver": "PMSS",
		"silvermetallic": "PMSS",
		"blue": "PPSB",
		"deepblue": "PPSB",
		"deepbluemetallic": "PPSB",
		"signatureblue": "PPSB",
		"metallicblue": "PMMB",
		"red": "PPMR",
		"redmulticoat": "PPMR",
		"signaturered": "PPSR",
		"brown": "PMAB",
		"metallicbrown": "PMAB",
		"titaniumcopper": "PPTI",
		"dolphingrey": "PMTG",
		"green": "PMSG",
		"metallicgreen": "PMSG",
		"quicksilver": "PN00",
		"stealthgrey": "PN01",
		"stealthgray": "PN01",
		"lunarsilver": "PN02",
		"midnightcherryred": "PR00",
		"ultrared": "PR01",
		"diamondblack": "PX02",
		"blackdiamond": "PX02",
		"frostblue": "PB00",
		"glacierblue": "PB01",
		"marineblue": "PB02",
	};

	// Cybertruck is unpainted stainless, whatever exterior_color says
	const UNPAINTED = { "ct": "PDD" };

	// Wheels on the configurator compositor need a matching trim and
	// interior, or it refuses to render.  trimAwd is used for dual-motor cars.
	const gemini19 = { wheel: "WY19B", trim: "MTY09", interior: "INPB0" };
	const aperture18 = { wheel: "WY18P", trim: "MTY61", trimAwd: "MTY77", interior: "IBB3" };
	const crossflow19 = { wheel: "WY19P", trim: "MTY60", trimAwd: "MTY48", interior: "IPB12" };
	const arachnid21 = { wheel: "WY21A", trim: "MTY70", interior: "IPB14" };
	const aero18 = { wheel: "W38B" };
	const sport19 = { wheel: "W39B" };
	const performance20 = { wheel: "W32P" };
	const gunpowder20 = { wheel: "W33D" };
	const photon18 = { wheel: "W38A", trim: "MT369", interior: "IPB2" };
	const nova19 = { wheel: "W39G", trim: "MT369", trimAwd: "MT370", interior: "IPB2" };
	const warp20 = { wheel: "W30A", trim: "MT371", interior: "IPB4" };
	const slipstream19 = { wheel: "WT19" };
	const aeroturbine19 = { wheel: "WTAS" };
	const turbine21 = { wheel: "WTSG" };
	const tempest19 = { wheel: "WS90", trim: "MTS13", interior: "IBE00" };
	const aeroturbine20 = { wheel: "WT20" };
	const slipstream20 = { wheel: "WTSC" };
	const turbine22 = { wheel: "WT22" };
	const onyx22 = { wheel: "WTUT" };
	const cyberstream20 = { wheel: "WX00", trim: "MTX10", interior: "IBE00" };

	// Each body generation, oldest first.  Wheel names match by prefix,
	// longest first, so "Pinwheel18CapKit" finds "pinwheel18".
	const GENERATIONS = [
		{
			model: "my", years: [2020, 2025], configurator: false,
			paints: ["PPSW", "PBSB", "PMNG", "PPSB", "PPMR", "PN00", "PN01", "PR00", "PR01"],
			wheels: {
				gemini19,
				apollo19: gemini19,
				apollo19metallicshadow: { ...gemini19, wheel: "WY19C" },
				induction20: { ...gemini19, wheel: "WY20P" },
				uberturbine21: { wheel: "WY21P", trim: "MTY12", interior: "INPB0" },
			},
			defaultWheel: gemini19,
		},
		{
			model: "my", years: [2025, null], configurator: true,
			paints: ["PN01", "PPSW", "PX02", "PN00", "PR01", "PB01", "PB02", "PPSB"],
			wheels: {
				aperture18,
				photon18: aperture18,
				crossflow19,
				helix20: { ...crossflow19, wheel: "WY20B" },
				arachnid21,
				uberturbine21: arachnid21,
			},
			defaultWheel: crossflow19,
		},
		{
			model: "m3", years: [2017, 2023], configurator: false,
			paints: ["PPSW", "PBSB", "PMNG", "PPSB", "PPMR", "PMBL", "PMSS"],
			wheels: {
				pinwheel18: aero18,
				aero18,
				pinwheelrefresh18: { wheel: "W40B" },
				stiletto19: sport19,
				sport19,
				sportwheel19: sport19,
				aeroturbine19: sport19,
				gemini19: sport19,
				stilettorefresh19: { wheel: "W41B" },
				stiletto20: performance20,
				stiletto20dark: { wheel: "W32D" },
				performance20,
				performancewheel20: performance20,
				uberturbine20: gunpowder20,
				zerog20: gunpowder20,
			},
			defaultWheel: aero18,
		},
		{
			model: "m3", years: [2024, null], configurator: true,
			paints: ["PN01", "PPSW", "PPSB", "PBSB", "PR01", "PN00", "PX02", "PB02"],
			wheels: {
				prismata18: { wheel: "W38C", trim: "MT367", trimAwd: "MT370", interior: "IBB4" },
				photon18,
				glider18: photon18,
				nova18: photon18,
				nova19,
				helix19: nova19,
				warp20,
				performance20: warp20,
			},
			defaultWheel: photon18,
		},
		{
			model: "ms", years: [2012, 2020], configurator: false,
			paints: ["PPSW", "PBCW", "PBSB", "PMBL", "PMNG", "PMSS", "PMTG", "PPTI",
				"PMAB", "PMSG", "PPSB", "PMMB", "PPMR", "PPSR"],
			wheels: {
				base19: slipstream19,
				slipstream19,
				aero19: aeroturbine19,
				aeroturbine19,
				turbine19: aeroturbine19,
				arachnid21grey: turbine21,
				arachnid21black: turbine21,
				arachnid21silver: aeroturbine19,
				super21gray: turbine21,
				super21silver: { wheel: "WTSS" },
				silver21: { wheel: "WT21" },
				charcoal21: turbine21,
				twinturbine21: { wheel: "WTTB" },
			},
			defaultWheel: slipstream19,
		},
		{
			model: "ms", years: [2021, 2025], configurator: true,
			paints: ["PPSW", "PBSB", "PMNG", "PPSB", "PPMR", "PN01", "PR01", "PN02"],
			wheels: {
				cardenio19: tempest19,
				tempest19,
				arachnid21: { ...tempest19, wheel: "WS11" },
			},
			defaultWheel: tempest19,
		},
		{
			model: "ms", years: [2025, null], configurator: true,
			paints: ["PN01", "PPSW", "PX02", "PB00", "PR01", "PN02"],
			wheels: {},
			defaultWheel: { wheel: "WS93", trim: "MTS22", interior: "IBE01" },
		},
		{
			model: "mx", years: [2015, 2020], configurator: false,
			paints: ["PPSW", "PBCW", "PBSB", "PMBL", "PMNG", "PMSS", "PPSB", "PPMR"],
			wheels: {
				aeroturbine20,
				slipstream20,
				slipstream20silver: aeroturbine20,
				aeroturbine22: turbine22,
				turbine22,
				turbine22dark: onyx22,
				onyx22black: onyx22,
			},
			defaultWheel: aeroturbine20,
		},
		{
			model: "mx", years: [2021, 2025], configurator: true,
			paints: ["PPSW", "PBSB", "PMNG", "PPSB", "PPMR"],
			wheels: {
				cyberstream20,
				newturbine22: { ...cyberstream20, wheel: "WX20" },
			},
			defaultWheel: cyberstream20,
		},
		{
			model: "mx", years: [2025, null], configurator: true,
			paints: ["PN01", "PPSW", "PX02", "PB00", "PR01", "PN02"],
			wheels: {},
			defaultWheel: { wheel: "WX02", trim: "MTX22", interior: "IBE01" },
		},
		{
			model: "ct", years: [2023, null], configurator: true,
			paints: ["PDD"],
			wheels: {},
			defaultWheel: { wheel: "WH0B", trim: "MTC03", interior: "IG01" },
		},
	];

	const ALL_PAINTS = new Set(Object.values(PAINT_CODES));

	function normalize(value) {
		return (value || "").toString().toLowerCase().replace(/[^a-z0-9]/g, "");
	}

	function isDualMotor(trimBadging) {
		let badge = normalize(trimBadging);
		return /\d+d$/.test(badge) || badge.startsWith("p");
	}

	function wheelFor(generation, wheelKey) {
		let match = Object.keys(generation.wheels).
			sort((a, b) => b.length - a.length).
			find(name => wheelKey && wheelKey.startsWith(name));
		return match ? generation.wheels[match] : null;
	}

	function builtIn(generation, year) {
		let [first, last] = generation.years;
		return first <= year && (last === null || year <= last);
	}

	// Picks the generation by model year when known, then by paint, then by
	// wheel.  A wheel no generation knows most likely comes from a newer car,
	// so the newest candidate shows its usual wheels.
	function pickGeneration(model, paint, wheelKey, year) {
		let candidates = GENERATIONS.filter(gen => gen.model === model);
		let ofYear = year ? candidates.filter(gen => builtIn(gen, year)) : [];
		if (ofYear.length > 0) {
			candidates = ofYear;
		}
		let painted = candidates.filter(gen => gen.paints.includes(paint));
		if (painted.length > 0) {
			candidates = painted;
		}
		for (const gen of candidates) {
			let spec = wheelFor(gen, wheelKey);
			if (spec) {
				return { gen, spec, wheelKnown: true };
			}
		}
		let gen = candidates[candidates.length - 1];
		return gen ? { gen, spec: gen.defaultWheel, wheelKnown: false } : null;
	}

	// The 10th character of the VIN is the model year
	function modelYearFromVin(vin) {
		const YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY";
		if (!vin || vin.length < 10) {
			return null;
		}
		let code = vin[9].toUpperCase();
		let index = YEAR_CODES.indexOf(code);
		if (index >= 0) {
			return 2010 + index;
		}
		return /^[1-9]$/.test(code) ? 2030 + Number(code) : null;
	}

	// config: { car_type, exterior_color, wheel_type, trim_badging,
	//           option_codes, model_year }
	// Returns { url, warnings }; url is null if the model is unknown.
	function build(config, size) {
		let warnings = [];
		let model = MODEL_CODES[normalize(config.car_type)];
		if (!model) {
			warnings.push("Unknown vehicle model: " + config.car_type);
			return { url: null, warnings };
		}

		let paint = UNPAINTED[model] || PAINT_CODES[normalize(config.exterior_color)];
		if (!paint) {
			warnings.push("Unknown vehicle color: " + config.exterior_color);
		}
		let wheelKey = normalize(config.wheel_type);
		let picked = pickGeneration(model, paint, wheelKey, config.model_year);
		let { gen, spec, wheelKnown } = picked;
		if (wheelKey && !wheelKnown) {
			warnings.push("Unknown wheel type: " + config.wheel_type);
		}
		if (!gen.paints.includes(paint)) {
			paint = null;
		}

		let trim = (spec.trimAwd && isDualMotor(config.trim_badging)) ?
			spec.trimAwd : spec.trim;
		let options;
		if (!gen.configurator && config.option_codes) {
			// The legacy compositor tolerates the car's full option list;
			// swap in the paint and wheel we know are right.
			let genWheels = new Set(
				[gen.defaultWheel, ...Object.values(gen.wheels)].map(w => w.wheel));
			options = config.option_codes.split(",").
				map(code => code.trim().replace(/^\$/, "")).
				filter(code => code);
			let hasWheel = options.some(code => genWheels.has(code));
			options = options.filter(code =>
				!(paint && ALL_PAINTS.has(code)) &&
				!(wheelKnown && genWheels.has(code)));
			if (paint) {
				options.push(paint);
			}
			if (wheelKnown || !hasWheel) {
				options.push(spec.wheel);
			}
		}
		else {
			// The configurator compositor refuses unknown or mismatched
			// options, so send exactly the set it needs.
			options = [trim, paint || gen.paints[0], spec.wheel, spec.interior].
				filter(code => code);
		}

		let params = [
			"model=" + gen.model,
			"view=STUD_3QTR",
			"size=" + (size || 400),
			"bkba_opt=1",
		];
		if (gen.configurator) {
			params.push("context=design_studio_2", "crop=0,0,0,0", "overlay=0");
			options = options.map(code => "$" + code);
		}
		params.push("options=" + options.join(","));

		let base = gen.configurator ? CONFIGURATOR_COMPOSITOR : LEGACY_COMPOSITOR;
		return { url: base + "?" + params.join("&"), warnings };
	}

	return { build, modelYearFromVin };
})();

if (typeof module !== "undefined") {
	module.exports = TeslaCompositor;
}
