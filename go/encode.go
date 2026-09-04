package whitebird

import (
	"bytes"
	"encoding"
	"fmt"
	"io"
	"mime/multipart"
	"net/url"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
)

// Parameter structs mark their fields with the tags "query", "header" and "form", holding the
// wire name and optionally ",omitempty". Nil pointers, nil interfaces and empty slices are
// never sent; other zero values are skipped only with omitempty. Slices repeat the parameter.

// encodeValues collects the fields of the struct v that carry tag.
func encodeValues(v any, tag string) (url.Values, error) {
	values := url.Values{}
	err := eachTagged(v, tag, func(name string, field reflect.Value) error {
		strs, err := formatValues(field)
		values[name] = append(values[name], strs...)
		return err
	})
	return values, err
}

// multipartBody encodes the "form" fields of the struct v as multipart/form-data. Fields
// holding an [io.Reader] become files, named after the reader if it has a Name method like
// [os.File].
func multipartBody(v any) encoder {
	return func() (string, []byte, error) {
		var buf bytes.Buffer
		w := multipart.NewWriter(&buf)
		err := eachTagged(v, "form", func(name string, field reflect.Value) error {
			if r, ok := field.Interface().(io.Reader); ok {
				return writeFile(w, name, r)
			}
			strs, err := formatValues(field)
			for _, s := range strs {
				if err == nil {
					err = w.WriteField(name, s)
				}
			}
			return err
		})
		if closeErr := w.Close(); err == nil {
			err = closeErr
		}
		return w.FormDataContentType(), buf.Bytes(), err
	}
}

func writeFile(w *multipart.Writer, name string, r io.Reader) error {
	filename := name
	if named, ok := r.(interface{ Name() string }); ok {
		filename = filepath.Base(named.Name())
	}
	part, err := w.CreateFormFile(name, filename)
	if err != nil {
		return err
	}
	_, err = io.Copy(part, r)
	return err
}

func eachTagged(v any, tag string, fn func(name string, field reflect.Value) error) error {
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
		spec, ok := rt.Field(i).Tag.Lookup(tag)
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
		if err := fn(name, field); err != nil {
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
