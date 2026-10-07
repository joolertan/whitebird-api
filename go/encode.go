package whitebird

import (
	"bytes"
	"cmp"
	"encoding"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/textproto"
	"net/url"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
)

// Parameter structs mark their fields with the tags "query", "header" and "form", holding the
// wire name and optionally ",omitempty". Nil pointers, nil interfaces and empty slices are
// never sent; other zero values are skipped only with omitempty. Slices repeat the parameter.
// In multipart bodies, the tag "mime" sets the content type of a part.

// encodeValues collects the fields of the struct v that carry tag.
func encodeValues(v any, tag string) (url.Values, error) {
	values := url.Values{}
	err := eachTagged(v, tag, func(name string, field reflect.Value, _ reflect.StructTag) error {
		strs, err := formatValues(field)
		values[name] = append(values[name], strs...)
		return err
	})
	return values, err
}

// multipartBody encodes the "form" fields of the struct v as multipart/form-data. Readers
// become file parts, named after the reader if it has a Name method like [os.File], and slices
// of readers one part per reader. Structs and maps become JSON parts, as do other values whose
// "mime" tag is a JSON media type, except strings, which are taken to be JSON already. Other
// values become text parts.
func multipartBody(v any) encoder {
	return func() (string, []byte, error) {
		var buf bytes.Buffer
		w := multipart.NewWriter(&buf)
		err := eachTagged(v, "form", func(name string, field reflect.Value, tag reflect.StructTag) error {
			return writePart(w, name, tag.Get("mime"), field)
		})
		if closeErr := w.Close(); err == nil {
			err = closeErr
		}
		return w.FormDataContentType(), buf.Bytes(), err
	}
}

func writePart(w *multipart.Writer, name, contentType string, field reflect.Value) error {
	switch v := field.Interface().(type) {
	case io.Reader:
		return writeFile(w, name, contentType, v)
	case []io.Reader:
		for _, r := range v {
			if err := writeFile(w, name, contentType, r); err != nil {
				return err
			}
		}
		return nil
	}
	for field.Kind() == reflect.Pointer || field.Kind() == reflect.Interface {
		field = field.Elem()
	}
	if contentType == "" && isObject(field.Type()) {
		contentType = "application/json"
	}
	if isJSON(contentType) {
		data := []byte(field.String())
		if field.Kind() != reflect.String {
			var err error
			if data, err = json.Marshal(field.Interface()); err != nil {
				return err
			}
		}
		return writeField(w, name, contentType, data)
	}
	strs, err := formatValues(field)
	for _, s := range strs {
		if err == nil {
			err = writeField(w, name, contentType, []byte(s))
		}
	}
	return err
}

func writeField(w *multipart.Writer, name, contentType string, data []byte) error {
	header := textproto.MIMEHeader{"Content-Disposition": {`form-data; name="` + quoteEscaper.Replace(name) + `"`}}
	if contentType != "" {
		header.Set("Content-Type", contentType)
	}
	part, err := w.CreatePart(header)
	if err == nil {
		_, err = part.Write(data)
	}
	return err
}

var quoteEscaper = strings.NewReplacer("\\", "\\\\", `"`, "\\\"")

func writeFile(w *multipart.Writer, name, contentType string, r io.Reader) error {
	filename := name
	if named, ok := r.(interface{ Name() string }); ok {
		filename = filepath.Base(named.Name())
	}
	header := textproto.MIMEHeader{
		"Content-Disposition": {multipart.FileContentDisposition(name, filename)},
		"Content-Type":        {cmp.Or(contentType, "application/octet-stream")},
	}
	part, err := w.CreatePart(header)
	if err != nil {
		return err
	}
	_, err = io.Copy(part, r)
	return err
}

var textMarshaler = reflect.TypeFor[encoding.TextMarshaler]()

// isObject reports whether values of type t encode as JSON objects or arrays of them.
func isObject(t reflect.Type) bool {
	for t.Kind() == reflect.Pointer {
		t = t.Elem()
	}
	if t.Implements(textMarshaler) || reflect.PointerTo(t).Implements(textMarshaler) {
		return false
	}
	switch t.Kind() {
	case reflect.Struct, reflect.Map:
		return true
	case reflect.Slice, reflect.Array:
		return isObject(t.Elem())
	}
	return false
}

func isJSON(contentType string) bool {
	mediaType, _, _ := strings.Cut(contentType, ";")
	mediaType = strings.TrimSpace(mediaType)
	return mediaType == "application/json" || strings.HasSuffix(mediaType, "+json")
}

func eachTagged(v any, tag string, fn func(name string, field reflect.Value, tags reflect.StructTag) error) error {
	rv := reflect.ValueOf(v)
	for rv.Kind() == reflect.Pointer {
		if rv.IsNil() {
			return nil
		}
		rv = rv.Elem()
	}
	if rv.Kind() != reflect.Struct {
		return nil
	}
	rt := rv.Type()
	for i := range rt.NumField() {
		tags := rt.Field(i).Tag
		spec, ok := tags.Lookup(tag)
		if !ok {
			continue
		}
		name, option, _ := strings.Cut(spec, ",")
		field := rv.Field(i)
		switch {
		case (field.Kind() == reflect.Pointer || field.Kind() == reflect.Interface) && field.IsNil():
			continue
		case option == "omitempty" && field.IsZero():
			continue
		}
		if err := fn(name, field, tags); err != nil {
			return fmt.Errorf("%s %s: %w", tag, name, err)
		}
	}
	return nil
}

func formatValues(v reflect.Value) ([]string, error) {
	switch v.Kind() {
	case reflect.Pointer, reflect.Interface:
		if v.IsNil() {
			return nil, nil
		}
		return formatValues(v.Elem())
	case reflect.Slice, reflect.Array:
		strs := make([]string, 0, v.Len())
		for i := range v.Len() {
			s, err := formatValue(v.Index(i))
			if err != nil {
				return nil, err
			}
			strs = append(strs, s)
		}
		return strs, nil
	}
	s, err := formatValue(v)
	return []string{s}, err
}

func formatValue(v reflect.Value) (string, error) {
	if m, ok := v.Interface().(encoding.TextMarshaler); ok {
		text, err := m.MarshalText()
		return string(text), err
	}
	switch v.Kind() {
	case reflect.String:
		return v.String(), nil
	case reflect.Bool:
		return strconv.FormatBool(v.Bool()), nil
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		return strconv.FormatInt(v.Int(), 10), nil
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return strconv.FormatUint(v.Uint(), 10), nil
	case reflect.Float32, reflect.Float64:
		return strconv.FormatFloat(v.Float(), 'f', -1, v.Type().Bits()), nil
	}
	return "", fmt.Errorf("unsupported type %s", v.Type())
}
