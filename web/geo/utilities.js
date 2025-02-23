const {
  polygon,
  getCoords,
  getType,
  featureEach,
  featureCollection,
  area,
  truncate,
  length,
  polygonToLineString,
} = require('@turf/turf');
const fs = require('fs');

function getPolygons(input) {
  /* Transform the feature collection of polygons and multi-polygons into a feature collection of polygons only */
  /* all helper functions should rely on its output */
  const handlePolygon = (feature, props) => polygon(getCoords(feature), props);
  const handleMultiPolygon = (feature, props) => getCoords(feature).map((coord) => polygon(coord, props));

  const polygons = [];
  let fc;
  if (getType(input) !== 'FeatureCollection') {
    fc = featureCollection([input]);
  } else {
    fc = input;
  }

  featureEach(fc, (feature) => {
    const type = getType(feature);
    switch (type) {
      case 'Polygon':
        polygons.push(handlePolygon(feature, feature.properties));
        break;
      case 'MultiPolygon':
        polygons.push(...handleMultiPolygon(feature, feature.properties));
        break;
      default:
        throw Error(`Encountered unhandled type: ${type}`);
    }
  });

  return truncate(featureCollection(polygons), { precision: 4 });
}

function isSliver(polygon, polArea, sliverRatio) {
  const lineStringLength = length(polygonToLineString(polygon));
  return Number(lineStringLength / polArea) > Number(sliverRatio);
}

function getHoles(fc, minArea, sliverRatio) {
  const holes = [];
  featureEach(fc, (ft) => {
    const coords = getCoords(ft);
    if (coords.length > 0) {
      for (let i = 0; i < coords.length; i++) {
        const pol = polygon([coords[i]]);
        const polArea = area(pol);
        if (polArea < minArea && !isSliver(pol, polArea, sliverRatio)) {
          holes.push(pol);
        }
      }
    }
  });
  return featureCollection(holes);
}

const fileExists = (fileName) => fs.existsSync(fileName);

const getJSON = (fileName, encoding = 'utf8') => JSON.parse(fs.readFileSync(fileName, encoding));

const writeJSON = (fileName, obj, encoding = 'utf8') =>
  fs.writeFileSync(fileName, JSON.stringify(obj), { encoding, flag: 'w' });

function log(message) {
  console.error('\x1b[31m%s\x1b[0m', `ERROR: ${message}`);
}

/**
 * Function to round a number to a specific amount of decimals.
 * @param {number} number - The number to round.
 * @param {number} decimals - Defaults to 2 decimals.
 * @returns {number} Rounded number.
 */
const round = (number, decimals = 2) => {
  return Math.round((number + Number.EPSILON) * 10 ** decimals) / 10 ** decimals;
};

/**
 * Rounds the all the coordinates in the geojson to a specific amount of decimals and returns a new geojson.
 * @param {GeoJSON} geojson
 * @param {int} precision
 * @returns
 */
const roundGeoPoints = (geojson, precision = 4) => {
  geojson.features.forEach((feature) => {
    feature.geometry.coordinates.forEach((group) => {
      group.forEach((area) => {
        area.forEach((point) => {
          point[0] = round(point[0], precision);
          point[1] = round(point[1], precision);
        });
      });
    });
  });
  return geojson;
};

module.exports = { getPolygons, getHoles, isSliver, writeJSON, getJSON, log, round, roundGeoPoints, fileExists };
