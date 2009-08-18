package aff4.storage;

import java.io.IOException;
import java.net.URL;
import java.net.URLDecoder;
import java.net.URLEncoder;
import java.text.ParseException;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.HashSet;
import java.util.List;
import java.util.TooManyListenersException;

import de.schlichtherle.util.zip.ZipEntry;
import de.schlichtherle.util.zip.ZipFile;

import aff4.container.StreamResolver;
import aff4.infomodel.Quad;
import aff4.infomodel.QuadList;
import aff4.infomodel.QuadStore;
import aff4.infomodel.TooManyValuesException;
import aff4.infomodel.serialization.MapResolver;
import aff4.infomodel.serialization.MultiHashPropertiesReader;
import aff4.infomodel.serialization.MultiHashSet;
import aff4.infomodel.serialization.Properties2Resolver;
import aff4.infomodel.serialization.PropertiesReader;
import aff4.infomodel.serialization.PropertiesResolver;


public class ReadOnlyZipVolume implements ZipVolume {
	String volumeURN = null;
	ArrayList<String> instances = null;
	QuadStore store = null;
	ZipFile zf = null;
	ArrayList<ZipEntry> propertiesEntries = null;
	ArrayList<ZipEntry> mapEntries = null;
	ArrayList<ZipEntry> otherEntries = null;
	String path;

	public ReadOnlyZipVolume(String path) throws IOException {
		zf = new ZipFile(path);
		volumeURN = zf.getComment();
		this.path = path;
		
	}
	
	public List<String> queryValues(String g, String s, String p) throws IOException, ParseException{
		QuadList res = query(g, s, p, null);
		return res.getObjects();
	}
	
	public String queryValue(String g, String s, String p) throws IOException, TooManyValuesException, ParseException{
		QuadList res = query(g, s, p, null);
		if (res.size() == 1){
			return res.getObjects().get(0);
		} else if (res.size() == 0) {
			return null;
		} else {
			throw new TooManyValuesException();
		}
	}
	
	
	public QuadList query(String g, String s, String p, String o) throws IOException, ParseException {
		if (store == null) {
			// first query we cache all of the results in the quadstore
			store = new QuadStore();
			QuadList res = queryNoCache(null, null, null, null);
			store.add(res);
		}
		return store.query(g, s, p, o);
		
	}
	

	public QuadList queryNoCache(String g, String s, String p, String o) throws IOException, ParseException {
		ZipEntry e = null;
		instances = new ArrayList<String>();
		Enumeration<ZipEntry> it = zf.entries();
		ArrayList<ZipEntry> entries = new ArrayList<ZipEntry>();
		while (it.hasMoreElements()) {
			e = it.nextElement();
			entries.add(e);
		}
		
		// run through once to pick up all the properties segments,
		// processing the properties segments as we go
		QuadList results = new QuadList();
		HashSet<ZipEntry> propertiesEntries = new HashSet<ZipEntry>();
		HashSet<ZipEntry> mapEntries = new HashSet<ZipEntry>();
		HashSet<ZipEntry> otherEntries = new HashSet<ZipEntry>();
		for (ZipEntry ent : entries) {
			if (ent.getName().endsWith("properties")) {
				List<Quad> result = queryPropertiesSegment(g,s,p,o,zf, ent);
				propertiesEntries.add(ent);
				results.addAll(result);
			} else if (ent.getName().endsWith("map")) {
				mapEntries.add(ent);
			} else {
				otherEntries.add(ent);
			}
		}

		// process any map entries
		for (ZipEntry ent : mapEntries) {
			List<Quad> result = queryMapSegment(g,s,p,o,zf, ent);
			results.addAll(result);
		}
		
		// for each image pull out metadata
		QuadStore qs = new QuadStore(results);
		for (String i : instances) {
			if (store.query(null, i, "aff4:type", "image").size() == 1) {
				// this is an image instance
				StreamResolver sr = new StreamResolver(this, otherEntries);
				QuadList result = sr.query(null, i, null, null);
				results.addAll(result);
			}
			
		}
		return results;
	}
	
	
	
	QuadList queryPropertiesSegment(String g, String s, String p, String o, ZipFile zf, ZipEntry e) throws IOException, ParseException {
		String path = e.getName();
		path = URLDecoder.decode(path);
		String instanceURN = null;
		
		
		if (path.endsWith("/properties")) {
			if (path.startsWith("urn:aff4")) {
				// regular instance
				int pos = path.indexOf("/properties");
				instanceURN = path.substring(0,pos);
				instances.add(instanceURN);
			} else {
				// it is a symolic name
				instanceURN = volumeURN + "/" + path.substring(0,path.indexOf("/"));
			}
		} else {
			// it is the volume properties segment
			instanceURN = volumeURN;
		}
		
		PropertiesResolver pr = new Properties2Resolver(zf.getInputStream(e), instanceURN + "/properties", instanceURN);
		QuadList res = pr.query(g, s, p, o);

		return res;
	}
	
	QuadList queryMapSegment(String g, String s, String p, String o, ZipFile zf, ZipEntry e) throws IOException, ParseException {
		String path = e.getName();
		path = URLDecoder.decode(path);
		String instanceURN = null;
		
		
		if (path.endsWith("/map")) {
			if (path.startsWith("urn:aff4")) {
				// regular instance
				int pos = path.indexOf("/map");
				instanceURN = path.substring(0,pos);
			} 
		}
		
		// get size of map
		List<Quad> r = queryPropertiesSegment(g, instanceURN, "aff4:size", o, zf, zf.getEntry(URLEncoder.encode(instanceURN)+"/properties"));
		long mapSize;
		
		if (r.size() == 1) {
			mapSize = Long.parseLong(r.get(0).getObject());
		} else {
			throw new RuntimeException("expected size attribute in " + instanceURN);
		}

		MapResolver pr = new MapResolver(instanceURN,mapSize, zf.getInputStream(e));
		QuadList res;
		if (s == null) {
			res = pr.query(s,p, o);
			//TODO: Check the below line
		} else if (s.startsWith(instanceURN + "[") || s.equals(instanceURN) ) {
			res = pr.query(s,p, o);
		} else {
			res = new QuadList();
		}
		
		for (Quad q : res) {
			q.setGraph(volumeURN);
			//q.setSubject(instanceURN);
		}
		return res;
	}

	public Reader open(String urn) throws IOException, ParseException {
		QuadList res = query(null, urn, "aff4:type", null);
		ArrayList<String> typeList = new ArrayList<String>();
		for (Quad q : res) {
			typeList.add(q.getObject());
		}
		
		if (typeList.contains("image")){
			return new StreamReader(this, urn);
		} else if (typeList.contains("map")){
			return new MapReader(this,urn);
		}
		
		return null;
	}
	
	public String getURN() {
		return volumeURN;
	}
	
	public void close() throws IOException {
		zf.close();
	}
	
	public String getPath() {
		return path;
	}
	
	public ZipFile getZipFile() {
		return zf;
	}
}


/*
Advanced Forensic Format v.4 http://www.afflib.org/

Copyright (C) 2009  Bradley Schatz <bradley@schatzforensic.com.au>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
*/