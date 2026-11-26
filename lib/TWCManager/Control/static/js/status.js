
$(document).ready(function() {
  $("#cancel_chargenow").click(function(e) {
    e.preventDefault();
    $.ajax({
      type: "POST",
      url: "/api/cancelChargeNow",
      data: {}
    });
  });
});

$(document).ready(function() {
  $("#send_start_command").click(function(e) {
    e.preventDefault();
    $.ajax({
      type: "POST",
      url: "/api/sendStartCommand",
      data: {}
    });
  });
});

$(document).ready(function() {
  $("#send_stop_command").click(function(e) {
    e.preventDefault();
    $.ajax({
      type: "POST",
      url: "/api/sendStopCommand",
      data: {}
    });
  });
});

// A VIN opens its vehicle's page; the spans are filled live, so the
// handler is delegated and reads the text at click time
$(document).on("click", ".VINClick", function () {
  var vin = $(this).text().trim();
  if (vin) {
    window.open("/vehicleDetail/" + encodeURIComponent(vin), "_blank");
  }
});
