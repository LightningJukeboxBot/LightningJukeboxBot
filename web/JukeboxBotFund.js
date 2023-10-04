    // Generate QR code with text value using a QR code generator library
    const qrCodeImage = document.getElementById("qr-code-image");
    const lnurl = qrCodeImage.getAttribute('lnurl');
    qrCodeImage.src = `https://jukebox.lighting/api/v1/qrcode/${encodeURIComponent(lnurl)}`;
    
    // Add event listener to copy data button
    const copyDataButton = document.querySelector(".copy-data");
    copyDataButton.addEventListener("click", () => {
      // Copy text value to clipboard
      const tempElement = document.createElement("textarea");
      tempElement.value = lnurl;
      document.body.appendChild(tempElement);
      tempElement.select();
      document.execCommand("copy");
      document.body.removeChild(tempElement);
    });

    qrCodeImage.addEventListener("click", () => {
      window.location.replace(`lightning:${encodeURIComponent(lnurl)}`);
    });

