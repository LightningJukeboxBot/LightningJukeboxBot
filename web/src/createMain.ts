import { MainSettings } from "./setup";
import { createDiv, setDisplayGrid, setHeight91cqh } from "./utils";

export const createMain = (settings: MainSettings) => {
  const container = createDiv();
  setHeight91cqh(container);
  container.id = settings.container.id;
  setDisplayGrid(container);
  container.style.gridTemplateRows = settings.container.style.gridTemplateRows;
  return container;
};
