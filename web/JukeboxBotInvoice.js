    // Generate QR code with text value using a QR code generator library
    const qrCodeImage = document.getElementById("qr-code-image");
    const invoice = qrCodeImage.getAttribute('invoice');
    qrCodeImage.src = `https://jukebox.lighting/api/v1/qrcode/${encodeURIComponent(invoice)}`;
    
    // Add event listener to copy data button
    const copyDataButton = document.querySelector(".copy-data");
    copyDataButton.addEventListener("click", () => {
      // Copy text value to clipboard
      const tempElement = document.createElement("textarea");
      tempElement.value = invoice;
      document.body.appendChild(tempElement);
      tempElement.select();
      document.execCommand("copy");
      document.body.removeChild(tempElement);
    });

    qrCodeImage.addEventListener("click", () => {
      window.location.replace(`lightning:${encodeURIComponent(invoice)}`);
    });

    // get payment hash and check payment status
const paymentHash = qrCodeImage.getAttribute('paymentHash')
var xmlhttp = new XMLHttpRequest();

xmlhttp.onreadystatechange = function() {
    if (this.readyState == 4 ) {	    
        var result = JSON.parse(this.responseText);
	if ( result['paid'] == true ) {
	    window.location.replace("/jukebox/assets/jukeboxbot_invoicepaid.html");
	}
    }
};

setInterval(function() {
    xmlhttp.open("GET", `/api/v1/payments/${paymentHash}`, true);
    xmlhttp.send();
},2000);
