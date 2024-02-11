// web/src/setup.ts
var vote = true;
var innerRed = `#ff8a8d`;
var darkRed = `#c50200`;
var superDark = `#720000`;
var greenLight = `#a5f782`;
var greencolor = `#73d04c`;
var greenDark = `#27821a`;
var blueLight = `#8ba5f6`;
var bluecolor = `#5e84fc`;
var blueDark = `#0d459d`;
var fontMid = `"1.3rem"`;
var tabwidth = 6;
var tabHeight = 7;
var footerHeight = 7;
var gridTemplate = "1fr 5fr 1fr";
var tabIconsSize = 32;
var medSection = 6;
var bigSection = 9;
var spaces = [medSection, medSection, bigSection];
var telegramLink = "https://tnl.productions";
var githubLink = "https://github.com/LightningJukeboxBot/LightningJukeboxBot";
var xLink = "https://tnl.productions";
var nrfmLink = "https://tnl.productions";

// web/src/utils.ts
var openPopUp = (url, target, features) => window.open(url, target, features);
var setMargin = (margin) => (element) => element.style.margin = margin;
var setHeight = (height) => (element) => element.style.height = height;
var setDisplay = (display) => (element) => element.style.display = display;
var setBorderRadius = (radius) => (element) => element.style.borderRadius = radius;
var setMargin0 = setMargin("0");
var setMargin1rem = setMargin("1rem");
var setHeight6cqh = setHeight(`${medSection}cqh`);
var setHeight9cqh = setHeight(`${bigSection}cqh`);
var setHeight91cqh = setHeight(`${100 - spaces.reduce((p, c) => p + c)}cqh`);
var setDisplayFlex = setDisplay("flex");
var setDisplayGrid = setDisplay("grid");
var setElementStyle = (element, text) => {
  setMargin("3px")(element);
  setBorderRadius("4px")(element);
  text.style.padding = "0.3rem";
  text.style.color = "black";
};
var createDiv = () => document.createElement("div");
var createSvg = (src, height = "1rem") => {
  const svg = document.createElement("img");
  svg.setAttribute("src", src);
  setHeight(height)(svg);
  return svg;
};
var createP = () => {
  const p = document.createElement("p");
  setMargin0(p);
  return p;
};
var createButton = (image) => {
  const button = document.createElement("button");
  button.style.border = "none";
  button.style.backgroundColor = "white";
  button.style.borderRadius = "4px";
  const svg = createSvg(image);
  svg.style.height = "1.3rem";
  button.appendChild(svg);
  return button;
};
var makeIconPath = (icon) => `../Assets/icons/${icon}.svg`;
var makeLinearGradient = (colora, colorb) => `linear-gradient(180deg,${colora},${colorb})`;

// web/src/index.ts
var getResults = () => {
  return new Array(25).fill("EMPTY");
};
var getQueue = () => {
  return new Array(25).fill("I WILL BE THERE");
};
var createUI = (currentqueue, results) => {
  const map = new Map;
  map.set("red-tab", innerRed);
  map.set("green-tab", greenLight);
  map.set("blue-tab", blueLight);
  const setElementColor = (element) => (color) => {
    element.style.color = map.get(color);
  };
  const setElementBgColor = (element) => (color) => {
    element.style.backgroundColor = map.get(color);
  };
  const redGradient = makeLinearGradient(darkRed, superDark);
  const alldarkred = makeLinearGradient(superDark, superDark);
  const greenGradient = makeLinearGradient(greencolor, greenDark);
  const alldarkgreen = makeLinearGradient(greenDark, greenDark);
  const blueGradient = makeLinearGradient(bluecolor, blueDark);
  const alldarkblue = makeLinearGradient(blueDark, blueDark);
  const gradientMap = new Map;
  gradientMap.set("red-tab", [redGradient, alldarkred]);
  gradientMap.set("red-tab-icon", [redGradient, alldarkred]);
  gradientMap.set("green-tab", [greenGradient, alldarkgreen]);
  gradientMap.set("green-tab-icon", [greenGradient, alldarkgreen]);
  gradientMap.set("blue-tab", [blueGradient, alldarkblue]);
  gradientMap.set("blue-tab-icon", [blueGradient, alldarkblue]);
  const iconpaths = new Map;
  iconpaths.set("red-tab", makeIconPath("red-search"));
  iconpaths.set("red-tab-icon", makeIconPath("red-search"));
  iconpaths.set("green-tab", makeIconPath("green-search"));
  iconpaths.set("green-tab-icon", makeIconPath("green-search"));
  iconpaths.set("blue-tab", makeIconPath("blue-search"));
  iconpaths.set("blue-tab-icon", makeIconPath("blue-search"));
  const placeholderColors = new Map;
  placeholderColors.set("red-tab", darkRed);
  placeholderColors.set("red-tab-icon", darkRed);
  placeholderColors.set("green-tab", greencolor);
  placeholderColors.set("green-tab-icon", greencolor);
  placeholderColors.set("blue-tab", bluecolor);
  placeholderColors.set("blue-tab-icon", bluecolor);
  const createWebui = () => {
    const container = createDiv();
    container.id = "webui";
    const setWebUIStyle = () => {
      const style = container.style;
      style.height = "100vh";
      style.containerType = "size ";
      style.fontFamily = "boucherie-block";
      style.color = "white";
    };
    setWebUIStyle();
    return container;
  };
  const createTabs = (settings) => {
    const container = createDiv();
    container.id = settings.container.id;
    setDisplayFlex(container);
    const setContainerStyle = () => {
      const style = container.style;
      const { position, height } = settings.container;
      style.position = position;
      style.height = height;
    };
    setContainerStyle();
    const red = createDiv();
    red.id = settings.redTab.id;
    const redsvg = createSvg(makeIconPath(settings.redTab.iconFilename), settings.tabSize);
    redsvg.id = settings.redTab.icon.id;
    red.appendChild(redsvg);
    red.style.backgroundColor = settings.redTab.bgColor;
    const green = createDiv();
    green.id = settings.greenTab.id;
    const greenp = createP();
    const greensvg = createSvg(makeIconPath(settings.greenTab.iconFilename), settings.tabSize);
    green.appendChild(greensvg);
    greensvg.id = settings.greenTab.icon.id;
    const gr = greencolor;
    greenp.textContent = gr;
    green.style.backgroundColor = settings.greenTab.bgColor;
    green.style.left = settings.greenTab.left;
    const blue = createDiv();
    blue.id = settings.blueTab.id;
    const bluesvg = createSvg(makeIconPath(settings.blueTab.iconFilename), settings.tabSize);
    bluesvg.id = settings.blueTab.icon.id;
    blue.appendChild(bluesvg);
    blue.style.backgroundColor = settings.blueTab.bgColor;
    blue.style.left = settings.blueTab.left;
    const setTabStyle = (tab, active = true) => {
      const style = tab.style;
      style.width = settings.width;
      style.position = settings.position;
      style.bottom = settings.bottom;
      style.height = settings.height(active);
      style.borderRadius = settings.borderRadius;
      style.display = settings.display;
      style.placeItems = settings.placeItems;
      style.transition = settings.transition;
    };
    const setRedCb = (cb) => {
      red.onclick = cb;
    };
    const setGreebCb = (cb) => {
      green.onclick = cb;
    };
    const setBlueCb = (cb) => {
      blue.onclick = cb;
    };
    const setTabsHeights2 = (color) => {
      [red, green, blue].forEach((e) => {
        if (e.id === color) {
          console.log(e.id);
          setTabStyle(e);
        } else {
          setTabStyle(e, false);
        }
      });
    };
    [red, green, blue].forEach((e) => setTabStyle(e));
    container.appendChild(red);
    container.appendChild(green);
    container.appendChild(blue);
    return [container, [setRedCb, setGreebCb, setBlueCb], setTabsHeights2];
  };
  const createDisplay = (settings) => {
    const container = createDiv();
    container.id = "display-area";
    container.style.height = settings.container.height;
    container.style.containerType = "size";
    const setColor = (color) => container.style.backgroundImage = gradientMap.get(color);
    return [container, setColor];
  };
  const createHeader = (songinfostext, settings) => {
    const container = createDiv();
    container.style.placeItems = settings.container.placeItems;
    container.style.placeContent = settings.container.placeContent;
    container.id = settings.container.id;
    setHeight9cqh(container);
    const nowPlaying = createP();
    const makeText = settings.makeText;
    nowPlaying.textContent = makeText(songinfostext);
    const setNowPlayingStyle = () => {
      const style = nowPlaying.style;
      const margintb = settings.nowPlaying.margintb;
      const marginlr = settings.nowPlaying.marginlr;
      style.paddingTop = margintb;
      style.paddingRight = marginlr;
      style.paddingBottom = margintb;
      style.paddingLeft = marginlr;
      style.borderTop = settings.nowPlaying.borders;
      style.borderBottom = settings.nowPlaying.borders;
      style.fontSize = fontMid;
    };
    const setNowPlayingTrack2 = (track) => {
      nowPlaying.textContent = makeText(track);
    };
    setNowPlayingStyle();
    setDisplayFlex(container);
    container.appendChild(nowPlaying);
    return [container, setNowPlayingTrack2];
  };
  const createInfos = (text, settings) => {
    const container = createDiv();
    container.id = settings.container.id;
    container.style.placeItems = settings.container.placeItems;
    container.style.placeContent = settings.container.placeContent;
    setHeight6cqh(container);
    setDisplayFlex(container);
    const infosvg = createSvg(makeIconPath(settings.iconFilename));
    const infos2 = createP();
    infosvg.style.cursor = settings.icon.cursor;
    const setOnClick = (cb) => infosvg.onclick = cb;
    const setText2 = (text2) => infos2.textContent = text2;
    infos2.textContent = setText2(text);
    infos2.style.fontSize = settings.p.fontSize;
    infos2.style.marginLeft = settings.p.marginLeft;
    infos2.style.color = settings.p.color;
    const setInfosTextColor2 = setElementColor(infos2);
    container.appendChild(infosvg);
    container.appendChild(infos2);
    return [container, setOnClick, setInfosTextColor2, setText2];
  };
  const createSearchbar = (iconFilename, settings) => {
    const container = createDiv();
    container.id = settings.container.id;
    const setContainerStyle = () => {
      setDisplayFlex(container);
      container.style.placeItems = settings.container.style.placeItems;
      container.style.placeContent = settings.container.style.placeContent;
      setHeight6cqh(container);
    };
    const search = createP();
    search.textContent = settings.search.textContent;
    const setSearchStyle = () => {
      search.style.fontSize = settings.search.style.fontSize;
      search.style.marginRight = settings.search.style.marginRight;
    };
    const inputcontainer = createDiv();
    inputcontainer.id = settings.inputContainer.id;
    const setInputContainerStyle = () => {
      const style = inputcontainer.style;
      style.position = settings.inputContainer.style.position;
    };
    const setInputStyle = () => {
      const style = input.style;
      style.border = settings.input.style.border;
      style.padding = settings.input.style.padding;
      style.width = settings.input.style.width;
      setBorderRadius(settings.input.style.borderRadius)(input);
      style.backgroundColor = settings.input.style.bgColor;
      const { key, value } = settings.input.style.property;
      style.setProperty(key, value);
      style.maxHeight = settings.input.style.maxHeight;
    };
    setInputContainerStyle();
    const input = document.createElement("input");
    input.placeholder = settings.input.placeHolder;
    inputcontainer.appendChild(input);
    const searchIcon = createSvg(makeIconPath(iconFilename));
    searchIcon.id = settings.searchIcon.id;
    const inputValueCb2 = (onclick) => {
      searchIcon.onclick = (e) => {
        onclick(input.value);
      };
    };
    const calculateTop = () => {
      const height = input.getBoundingClientRect().height;
      const iconHeight = searchIcon.getBoundingClientRect().height;
      const delta = height - iconHeight;
      if (height < iconHeight) {
        return delta / 2;
      } else if (height > iconHeight) {
        return delta / 2;
      } else {
        return 0;
      }
    };
    const setIconStyle = () => {
      const style = searchIcon.style;
      style.position = settings.searchIcon.style.position;
      style.right = settings.searchIcon.style.right;
      style.height = settings.searchIcon.style.height;
      style.cursor = settings.searchIcon.style.cursor;
    };
    const setTop = (top) => {
      const style = searchIcon.style;
      style.top = settings.searchIcon.style.top(top.toString());
    };
    const setIconSrc2 = (src) => searchIcon.setAttribute("src", iconpaths.get(src));
    window.onload = () => {
      setIconStyle();
      const top = calculateTop();
      setTop(top);
    };
    window.onresize = () => {
      const top = calculateTop();
      setTop(top);
    };
    inputcontainer.appendChild(searchIcon);
    setContainerStyle();
    setSearchStyle();
    setInputStyle();
    container.appendChild(search);
    container.appendChild(inputcontainer);
    const setPlaceHolderColor = (color) => input.style.setProperty(settings.input.style.property.key, placeholderColors.get(color));
    return [container, setPlaceHolderColor, inputValueCb2, setIconSrc2];
  };
  const createMain = (settings) => {
    const container = createDiv();
    setHeight91cqh(container);
    container.id = settings.container.id;
    setDisplayGrid(container);
    container.style.gridTemplateRows = settings.container.style.gridTemplateRows;
    return container;
  };
  const createSearchResult = (settings) => {
    const container = createDiv();
    container.id = settings.container.id;
    container.style.overflow = settings.container.style.overflow;
    container.style.backgroundColor = settings.container.style.bgColor;
    setBorderRadius(settings.container.style.borderRadius)(container);
    setMargin1rem(container);
    const createResult = (result, i) => {
      const container2 = createDiv();
      container2.setAttribute(settings.container.attribute.key, i.toString());
      container2.style.backgroundColor = settings.container.result.style.backgroundColor;
      const text = createP();
      setElementStyle(container2, text);
      text.textContent = result;
      container2.appendChild(text);
      const setSearchResultCb = (cb) => {
        container2.onclick = cb;
      };
      return [container2, setSearchResultCb];
    };
    const setResultBackgroundColor2 = setElementBgColor(container);
    const showResult2 = (res) => {
      const resultsElements = res.map(createResult);
      resultsElements.forEach((e) => container.appendChild(e[0]));
      return resultsElements;
    };
    if (results) {
      const resultsElements = showResult2(results);
      return [
        container,
        resultsElements.map((e) => e[1]),
        setResultBackgroundColor2,
        showResult2
      ];
    } else {
      return [container, [], setResultBackgroundColor2, showResult2];
    }
  };
  const createQueue = (currentqueue2, settings) => {
    const container = createDiv();
    container.style.overflow = settings.container.style.overflow;
    setDisplayGrid(container);
    container.id = settings.container.id;
    const queueheader = createDiv();
    queueheader.id = settings.queueheader.id;
    setDisplayGrid(queueheader);
    queueheader.style.placeItems = settings.queueheader.style.placeItems;
    const createVote = () => {
      const vote2 = createP();
      vote2.textContent = settings.vote.textContent;
      vote2.style.color = innerRed;
      vote2.style.fontSize = settings.vote.style.fontSize;
      return vote2;
    };
    const voteL = createVote();
    const voteR = createVote();
    const setVoteColor2 = (color) => {
      [voteL, voteR].forEach((e) => setElementColor(e)(color));
    };
    const queue2 = createP();
    queue2.textContent = settings.queue.textContent;
    if (vote) {
      queueheader.style.gridTemplateColumns = gridTemplate;
      queueheader.style.margin = settings.queueheader.style.margin;
      queueheader.appendChild(voteL);
      queueheader.appendChild(queue2);
      queueheader.appendChild(voteR);
    } else {
      queueheader.appendChild(queue2);
    }
    const content = createDiv();
    setMargin1rem(content);
    content.id = settings.content.id;
    content.style.overflow = settings.content.style.overflow;
    content.style.backgroundColor = settings.content.style.backgroundColor;
    const setQueueBackgroundColor = setElementBgColor(content);
    setBorderRadius(settings.content.style.borderRadius)(content);
    const createQueueElement = (infostext, i) => {
      const element = createDiv();
      const infos2 = createP();
      infos2.style.backgroundColor = settings.infos.style.backgroundColor;
      infos2.style.width = settings.infos.style.width;
      setBorderRadius(settings.infos.style.borderRadius)(infos2);
      const margins = settings.infos.style.magins;
      infos2.style.marginLeft = margins;
      infos2.style.marginRight = margins;
      element.id = settings.infos.element.id;
      element.setAttribute(settings.infos.element.attribute.key, i.toString());
      infos2.textContent = infostext;
      setElementStyle(element, infos2);
      if (vote) {
        setDisplayGrid(element);
        element.style.gridTemplateColumns = gridTemplate;
        const upButton = createButton(makeIconPath(settings.infos.upvoteFilename));
        const setUpButtonCb = (cb) => {
          upButton.onclick = cb;
        };
        const downButton = createButton(makeIconPath(settings.infos.downvoteFilename));
        const setDownButtonCb = (cb) => {
          downButton.onclick = cb;
        };
        element.appendChild(downButton);
        element.appendChild(infos2);
        element.appendChild(upButton);
        infos2.style.placeSelf = settings.infos.style.placeSelf;
        return [element, setDownButtonCb, setUpButtonCb];
      } else {
        element.appendChild(infos2);
        return [element];
      }
    };
    const tracksnumber = createP();
    const tracks = 25;
    tracksnumber.textContent = settings.tracksNumber.textContent(tracks.toString());
    tracksnumber.style.placeSelf = settings.tracksNumber.style.placeSelf;
    tracksnumber.style.color = innerRed;
    tracksnumber.style.fontSize = settings.tracksNumber.style.fontSize;
    const setTrackInfoColor = (color) => setElementColor(tracksnumber)(color);
    content.style.marginTop = settings.content.style.marginTop;
    content.style.marginBottom = settings.content.style.marginBottom;
    const queuelements = currentqueue2.map(createQueueElement);
    queuelements.forEach((e) => content.appendChild(e[0]));
    container.appendChild(queueheader);
    container.appendChild(content);
    container.appendChild(tracksnumber);
    return [
      container,
      [queuelements.map((e) => e[1]), queuelements.map((e) => e[2])],
      setQueueBackgroundColor,
      setVoteColor2,
      setTrackInfoColor
    ];
  };
  const createFooter = (icons, settings) => {
    const container = createDiv();
    container.id = settings.container.id;
    const subcontainer = createDiv();
    subcontainer.id = settings.container.subcontainer.id;
    container.appendChild(subcontainer);
    setDisplayFlex(container);
    setDisplayGrid(subcontainer);
    const setContainerStyle = () => {
      const style = container.style;
      style.height = settings.container.style.height;
      style.placeContent = settings.container.style.placeContent;
      style.placeItems = settings.container.style.placeItems;
    };
    const setSubContainerStyle = () => {
      const style = subcontainer.style;
      style.width = settings.container.subcontainer.style.width;
      style.gridTemplateColumns = settings.container.subcontainer.style.gridTemplateColumns;
      style.placeSelf = settings.container.subcontainer.style.placeSelf;
      style.placeItems = settings.container.subcontainer.style.placeItems;
    };
    setContainerStyle();
    setSubContainerStyle();
    const setColor = (color) => container.style.backgroundImage = gradientMap.get(color)[1];
    const elements = icons.map((e) => {
      const a = document.createElement("a");
      const svg = createSvg(e[0], settings.icons.style.height);
      a.appendChild(svg);
      a.style.cursor = settings.icons.style.cursor;
      a.setAttribute(settings.icons.style.attribute1.key, e[1]);
      a.setAttribute(settings.icons.style.attribute2.key, settings.icons.style.attribute2.value);
      return a;
    });
    elements.forEach((e, i, a) => {
      subcontainer.appendChild(e);
    });
    return [container, setColor];
  };
  const webui = createWebui();
  const [tabs, tabEvents, setTabsHeights] = createTabs({
    tabSize: `${tabIconsSize}px`,
    height: (active) => active ? "100%" : "60%",
    width: tabwidth + `rem`,
    position: "absolute",
    bottom: "0",
    borderRadius: "5px 5px 0 0",
    display: "grid",
    placeItems: "center",
    transition: "height 0.5s ease",
    container: {
      id: "tab-area",
      position: "relative",
      height: tabHeight + "cqh"
    },
    redTab: {
      id: "red-tab",
      iconFilename: "Red-tab-icon",
      icon: { id: "red-tab-icon" },
      bgColor: darkRed
    },
    greenTab: {
      id: "green-tab",
      iconFilename: "green-tab-icon",
      icon: { id: "green-tab-icon" },
      bgColor: greencolor,
      left: tabwidth + `rem`
    },
    blueTab: {
      id: "blue-tab",
      iconFilename: "blue-tab-icon",
      icon: { id: "blue-tab-icon" },
      bgColor: bluecolor,
      left: tabwidth * 2 + `rem`
    }
  });
  const [display, setDisplayColor] = createDisplay({
    container: {
      id: "display-area",
      height: 100 - [tabHeight, footerHeight].reduce((p, c) => p + c) + "cqh",
      containerType: "size"
    }
  });
  const [header, setNowPlayingTrack] = createHeader("NIRVANA - SMELLS LIKE TEEN SPIRIT", {
    container: {
      id: "jb-header",
      placeItems: "center",
      placeContent: "space-evenly"
    },
    nowPlaying: {
      margintb: "0.3rem",
      marginlr: "0.2rem",
      borders: "solid 1px white"
    },
    makeText: (track) => `NOW PLAYING: ${track}`
  });
  const [infos, setInfosOnClick, setInfosTextColor, setText] = createInfos("21 SATS PER TRACK & PER DOWN VOTE", {
    container: {
      id: "jukebox-infos",
      placeItems: "center",
      placeContent: "center"
    },
    iconFilename: "info",
    icon: { cursor: "pointer" },
    p: { fontSize: "0.8rem", marginLeft: "0.5rem", color: innerRed }
  });
  const [searchbar, setPlaceholderColor, inputValueCb, setIconSrc] = createSearchbar("red-search", {
    container: {
      id: "search-bar",
      style: { placeItems: "center", placeContent: "center" }
    },
    search: {
      textContent: "SEARCH",
      style: { marginRight: "0.5rem", fontSize: fontMid }
    },
    inputContainer: { id: "search-input", style: { position: "relative" } },
    input: {
      placeHolder: "ARTIST TRACK + TITLE",
      style: {
        border: "none",
        padding: "0.5rem",
        width: "70cqw",
        borderRadius: "15px",
        bgColor: "white",
        property: { key: "--c", value: innerRed },
        maxHeight: "35px"
      }
    },
    searchIcon: {
      id: "searchIcon",
      style: {
        position: "absolute",
        right: "0",
        height: "40px",
        cursor: "pointer",
        top: (top) => `${top}px`
      }
    }
  });
  const main = createMain({
    container: { id: "main", style: { gridTemplateRows: "1fr 1.4fr" } }
  });
  const [
    searchResults,
    searchResultCbSetters,
    setResultBackgroundColor,
    showResult
  ] = createSearchResult({
    container: {
      id: "results-area",
      style: { overflow: "scroll", bgColor: innerRed, borderRadius: "4px" },
      attribute: { key: "key" },
      result: {
        style: { backgroundColor: "white" }
      }
    },
    results
  });
  const [queue, votesCbs, setQueueBgColor, setVoteColor, setTracksInfoColor] = createQueue(currentqueue, {
    container: { style: { overflow: "hidden" }, id: "queue-area" },
    queueheader: {
      id: "queue-header",
      style: { placeItems: "center", margin: "0 1rem" }
    },
    vote: {
      textContent: "VOTE",
      style: { color: innerRed, fontSize: "0.8rem" }
    },
    queue: { textContent: "QUEUE" },
    content: {
      id: "content-area",
      style: {
        overflow: "scroll",
        backgroundColor: innerRed,
        borderRadius: "4px",
        marginTop: "0.2rem",
        marginBottom: "0.2rem"
      }
    },
    infos: {
      style: {
        backgroundColor: "white",
        width: "-webkit-fill-available",
        borderRadius: "4px",
        magins: "2px",
        placeSelf: "center"
      },
      element: {
        id: "queue-el",
        attribute: { key: "key" }
      },
      upvoteFilename: "upvote-arrow",
      downvoteFilename: "downvote-arrow"
    },
    tracksNumber: {
      textContent: (tracks) => `(${tracks} TRACKS QUEUED)`,
      style: { placeSelf: "center", color: innerRed, fontSize: "0.8rem" }
    }
  });
  const [footer, setFooterColor] = createFooter([
    [makeIconPath("telegram-icon"), telegramLink],
    [makeIconPath("github-cion"), githubLink],
    [makeIconPath("x-icon"), xLink],
    [makeIconPath("nrfm-icon"), nrfmLink]
  ], {
    container: {
      id: "footer-container",
      subcontainer: {
        id: "footer-subcontainer",
        style: {
          gridTemplateColumns: "1fr 1fr 1fr 1fr",
          placeSelf: "center",
          placeItems: "center",
          width: "90cqw"
        }
      },
      style: {
        height: footerHeight + `cqh`,
        placeContent: "center",
        placeItems: "center"
      }
    },
    icons: {
      style: {
        height: "24px",
        cursor: "pointer",
        attribute1: { key: "href" },
        attribute2: { key: "target", value: "_blank" },
        placeSelf: "center",
        palceItems: "center"
      }
    }
  });
  const activeTab = "red-tab";
  setMargin0(body);
  setDisplayColor(activeTab);
  setFooterColor(activeTab);
  setPlaceholderColor(activeTab);
  setTabsHeights(activeTab);
  webui.appendChild(tabs);
  webui.appendChild(display);
  display.appendChild(header);
  display.appendChild(infos);
  display.appendChild(searchbar);
  display.appendChild(main);
  main.appendChild(searchResults);
  main.appendChild(queue);
  webui.appendChild(footer);
  const setActive = (tab) => {
    setDisplayColor(tab);
    setFooterColor(tab);
    setIconSrc(tab);
    setPlaceholderColor(tab);
    setInfosTextColor(tab);
    setResultBackgroundColor(tab);
    setQueueBgColor(tab);
    setVoteColor(tab);
    setTracksInfoColor(tab);
    setTabsHeights(tab);
  };
  tabEvents.forEach((es) => es((e) => {
    const element = e.target;
    setActive(element.id);
  }));
  const searchResultCb = (cb) => {
    if (searchResultCbSetters.length !== 0)
      searchResultCbSetters.forEach((e) => e(cb));
  };
  const setVotesCb = (down, up) => {
    const downs = votesCbs[0];
    const ups = votesCbs[1];
    downs.forEach((d) => d(down));
    ups.forEach((d) => d(up));
  };
  setInfosOnClick(async (e) => {
    openPopUp("http://127.0.0.1:5500/web/popup.html", "_blank", "width=286, height=466");
  });
  return [
    webui,
    inputValueCb,
    searchResultCb,
    setVotesCb,
    showResult,
    setNowPlayingTrack,
    setText
  ];
};
var body = document.body;
var results = getResults();
var queue = getQueue();
var [
  ui,
  inputValueCb,
  searchResultCb,
  setVotesCb,
  showResult,
  setNowPlayingTrack,
  setText
] = createUI(queue);
body.prepend(ui);
inputValueCb((e) => {
  console.log(e);
  showResult(results);
});
searchResultCb((e) => console.log(e.currentTarget));
setVotesCb((e) => console.log(e.currentTarget), (e) => console.log(e.currentTarget));
setTimeout(() => setNowPlayingTrack("Gotek - Antistress"), 3000);
setTimeout(() => setText("now its fucking 420 sats per track!"), 6000);
