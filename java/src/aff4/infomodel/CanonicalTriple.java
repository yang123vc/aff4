package aff4.infomodel;


public class CanonicalTriple extends Object implements Comparable {
	public String subject;
	public String predicate;
	public String object;

	public CanonicalTriple(String s, String p, String o) {
		this.subject = s;
		this.predicate = p;
		this.object = o;
	}

	public CanonicalTriple(Quad q) {
		this.subject = q.getSubject();
		this.predicate = q.getPredicate();
		this.object = q.getObject();
	}

	public CanonicalTriple(CanonicalTriple st) {
		this.object = st.object;
		this.predicate = st.predicate;
		this.subject = st.subject;

	}

	public int compareTo(Object arg) throws ClassCastException {
		String current = subject.toString() + predicate.toString()
				+ object.toString();
		CanonicalTriple cs = (CanonicalTriple) arg;
		String ext = cs.subject.toString() + cs.predicate.toString()
				+ cs.object.toString();
		return current.compareTo(ext);
	}

	public String createTripleString() {
		return subject.toString() + " " + predicate.toString() + " "
				+ object.toString();
	}
}

