// Draws each <img class="vehicleImage"> as its car, from the appearance the
// car reported (data-appearance) and its VIN. A car that never reported one
// is drawn from its VIN alone, with that body's usual paint and wheels.
// live.js calls window.drawVehicleImage for pictures it adds after load.
window.drawVehicleImage = function (img) {
  var VIN_MODELS = { "3": "model3", "Y": "modely", "S": "models", "X": "modelx", "C": "cybertruck" };

  var vin = (img.dataset.vin || "").toUpperCase();
  var config = JSON.parse(img.dataset.appearance || "{}");
  config.car_type = config.car_type || VIN_MODELS[vin.charAt(3)];
  config.model_year = TeslaCompositor.modelYearFromVin(vin);

  var url = config.car_type && TeslaCompositor.build(config, img.dataset.size).url;
  if (url) {
    img.onerror = function () { img.hidden = true; };
    img.src = url;
    img.hidden = false;
  }
};

$(function () {
  $("img.vehicleImage").each(function () {
    window.drawVehicleImage(this);
  });
});
